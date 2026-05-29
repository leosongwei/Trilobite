import json
from typing import Any


def estimate_tokens(text: str) -> int:
    """Character-based token estimation: ~4 ASCII chars per token, ~1 CJK char per token."""
    ascii_count = 0
    non_ascii_count = 0
    for ch in text:
        if ord(ch) <= 127:
            ascii_count += 1
        else:
            non_ascii_count += 1
    return (ascii_count + 3) // 4 + non_ascii_count


def estimate_tokens_for_message(msg: dict) -> int:
    """Estimate tokens in a single message."""
    total = estimate_tokens(msg.get("role", ""))
    content = msg.get("content") or ""
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                total += estimate_tokens(part.get("text", ""))
    reasoning = msg.get("reasoning_content") or ""
    if reasoning:
        total += estimate_tokens(reasoning)
    for tc in msg.get("tool_calls", []):
        total += estimate_tokens(tc.get("function", {}).get("name", ""))
        total += estimate_tokens(tc.get("function", {}).get("arguments", ""))
    # tool messages carry tool_call_id
    if msg.get("role") == "tool":
        total += estimate_tokens(msg.get("tool_call_id", ""))
    return total


def estimate_tokens_for_messages(messages: list[dict]) -> int:
    """Estimate total tokens for a list of messages."""
    return sum(estimate_tokens_for_message(m) for m in messages)


def estimate_tokens_for_tools(tools: list[dict]) -> int:
    """Estimate tokens used by tool definitions."""
    total = 0
    for tool in tools:
        total += estimate_tokens(json.dumps(tool, ensure_ascii=False))
    return total
