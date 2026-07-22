from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from src.trilobite.config import load_compaction_prompt
from src.trilobite.tokens import estimate_tokens_for_messages, estimate_tokens_for_tools
from src.trilobite.tool_call import get_tool_definitions

if TYPE_CHECKING:
    from src.trilobite.agent import Agent


def can_split_after(messages: list[dict], index: int) -> bool:
    if index < 0 or index >= len(messages) - 1:
        return False
    prev = messages[index]
    after = messages[index + 1]
    if prev.get("role") == "user":
        return False
    if prev.get("role") == "assistant" and prev.get("tool_calls"):
        return False
    if after.get("role") == "tool":
        return False
    return True


def find_compact_boundary(messages: list[dict], max_recent: int = 6) -> int:
    if len(messages) <= 3:
        return -1
    min_keep = min(max_recent, len(messages) - 2)
    for keep in range(2, min_keep + 1):
        split = len(messages) - keep - 1
        if split >= 0 and can_split_after(messages, split):
            return split
    return -1


async def compact_if_needed(agent: Agent) -> bool:
    """Check if compaction is needed and perform it. Returns True if compacted."""
    if not _should_compact(agent):
        return False

    # System message is always at index 0; skip it for boundary finding
    raw = agent.history.raw
    conv_history = raw[1:] if raw and raw[0].get("role") == "system" else raw

    boundary = find_compact_boundary(conv_history)
    if boundary < 1:
        return False

    compact_messages = conv_history[: boundary + 1]
    recent_messages = conv_history[boundary + 1 :]

    # Reconstruct system message from current config during compaction
    system_msg = {"role": "system", "content": agent.system_prompt + agent.working_context}
    prompt = load_compaction_prompt()

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

    messages = [
        system_msg,
        *compact_messages,
        {"role": "user", "content": prompt},
    ]

    await agent._send_stream_event({"type": "status", "text": "compacting context..."})

    try:
        response = await agent.client.chat.completions.create(
            model=agent.model,
            messages=messages,
            stream=False,
        )
        summary = response.choices[0].message.content or ""
    except Exception:
        summary = "[compaction failed - older context dropped]"

    agent._compacted_summary = summary
    summary_msg = f"[Context summary]\n{summary}"

    agent.history.replace_all([
        system_msg,
        {"role": "user", "content": summary_msg},
        {"role": "assistant", "content": "Understood. Continuing with the task."},
        *recent_messages,
    ])
    agent._token_count = 0
    agent._token_covered = 0
    return True


def _should_compact(agent: Agent) -> bool:
    tools = get_tool_definitions()
    pending = agent.history.raw[agent._token_covered :]
    estimated = (
        agent._token_count
        + estimate_tokens_for_messages(pending)
        + estimate_tokens_for_tools(tools)
    )
    threshold = int(agent.max_context_tokens * agent.compaction_trigger_ratio)
    return estimated >= threshold
