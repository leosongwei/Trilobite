COMPACTION_INSTRUCTION = """
You are compacting conversation history to save context space. Your task is to summarize the conversation so far, preserving critical information.

Output text only — do NOT call any tools and NEVER prefix your response with tool calls.

What to preserve (in order of importance):
1. Current task state — what is being worked on right now
2. Errors encountered and their solutions
3. Code changes made (final versions, not intermediate attempts)
4. Project structure, dependencies, environment
5. Design decisions and rationale
6. Pending todo items
7. All user messages (summarize the requests, not verbatim)

Output format:
```
[Context Summary]
{summary text — free-form, include the important details listed above}
```
"""


def can_split_after(messages: list[dict], index: int) -> bool:
    """Check if it's safe to split history after position `index`.
    Cannot split after a user message (would leave it unanswered).
    Cannot split after an assistant message with pending tool calls.
    Cannot split so next message is a tool result (orphans the tool result).
    """
    if index < 0 or index >= len(messages) - 1:
        return False

    prev = messages[index]
    after = messages[index + 1]

    # Don't leave a user message unanswered
    if prev.get("role") == "user":
        return False

    # Don't orphan tool calls
    if prev.get("role") == "assistant" and prev.get("tool_calls"):
        return False

    # Don't orphan tool results
    if after.get("role") == "tool":
        return False

    return True


def find_compact_boundary(messages: list[dict], max_recent: int = 6) -> int:
    """Find a safe split point. Returns the index of the last message to compact,
    or -1 if no safe split exists."""
    if len(messages) <= 3:
        return -1
    # Try to preserve at most max_recent recent messages, find safe split
    min_keep = min(max_recent, len(messages) - 2)
    for keep in range(2, min_keep + 1):
        split = len(messages) - keep - 1
        if split >= 0 and can_split_after(messages, split):
            return split
    return -1
