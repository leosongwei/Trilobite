"""Group session (multi-agent chat channel) helpers.

A group session is a channel shared by the user and N member agents. The
user posts instructions to the channel; every member sees them; members talk
to the user and to each other through the ``send_to_group`` tool. This module
holds the small pieces of that machinery that are pure data/logic: the
member-name pool, name picking, the recipient model, and the formatting of a
channel message as it lands in a member's history.

The orchestration itself (spawning members, fanning messages out) lives on
``Agent``; the tool definition lives in ``tool_call.py``.
"""

from __future__ import annotations

# Human names for group members. Each member gets a distinct one so the
# channel (and every member's system prompt) can refer to teammates by name.
GROUP_NAME_POOL: tuple[str, ...] = (
    "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Henry",
    "Ivy", "Jack", "Karen", "Leo", "Mona", "Nick", "Olivia", "Pete",
    "Quinn", "Rosa", "Sam", "Tina", "Uma", "Victor", "Wendy", "Xavier",
)

#: Recipient names with special meaning in ``send_to_group``.
GROUP_USER = "user"
GROUP_ALL = "all"

#: Allowed number of group members (the creation dialog offers this range).
GROUP_MIN_SIZE = 1
GROUP_MAX_SIZE = 16


def validate_group_size(count: object) -> int | str:
    """Validate a member count from the client.

    Returns the confirmed int, or an error message.
    """
    if not isinstance(count, int) or isinstance(count, bool):
        return "count must be an integer"
    if not (GROUP_MIN_SIZE <= count <= GROUP_MAX_SIZE):
        return f"count must be between {GROUP_MIN_SIZE} and {GROUP_MAX_SIZE}"
    return count


def pick_member_names(count: int, taken: set[str] | None = None) -> list[str]:
    """Pick ``count`` distinct member names.

    Names come from :data:`GROUP_NAME_POOL` in order, skipping names already
    in use by other groups in the same UI; once the pool is exhausted a
    numeric suffix keeps names unique ("Alice 2").
    """
    taken = set(taken or ())
    names: list[str] = []
    # First pass: fresh names from the pool, in pool order.
    for base in GROUP_NAME_POOL:
        if len(names) >= count:
            break
        if base not in taken:
            names.append(base)
    # Second pass: numbered variants for the remainder ("Alice 2", ...).
    n = 2
    while len(names) < count:
        for base in GROUP_NAME_POOL:
            if len(names) >= count:
                break
            candidate = f"{base} {n}"
            if candidate not in taken and candidate not in names:
                names.append(candidate)
        n += 1
    return names


def format_group_message(sender: str, text: str) -> str:
    """Shape a channel message as it lands in a member's history.

    Members receive user/teammate messages as ordinary user messages tagged
    with the sender, so the model can tell who said what. The user is
    ``"user"``, teammates their member name.
    """
    return f"[from {sender}]\n{text}"


def resolve_recipients(
    to: str,
    sender_name: str,
    members: dict[str, str],
) -> tuple[list[str], str]:
    """Resolve a ``send_to_group`` recipient to member names.

    ``members`` maps member name -> session id. Returns (target names,
    error). An unknown name lists the valid recipients so the model can
    correct itself in one step.
    """
    to = (to or "").strip()
    if to == GROUP_USER:
        return [], ""
    if to == GROUP_ALL:
        return [name for name in members if name != sender_name], ""
    if to in members and to != sender_name:
        return [to], ""
    peers = ", ".join(name for name in members if name != sender_name)
    return [], (
        f"Error: unknown recipient '{to}'. Valid recipients are: "
        f"'{GROUP_USER}', '{GROUP_ALL}', or a teammate's name ({peers})."
    )
