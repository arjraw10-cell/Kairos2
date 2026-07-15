"""Conversation-history repair helpers used by the interactive frontends.

Saved chats can end at several points in an agent turn: before a response,
after an assistant tool-call message, or after only some of that message's
tool results were written.  This module turns those states into an API-valid
history and tells the caller whether it should continue the unfinished turn.
"""

import json
from typing import Any


_SCREENSHOT_PREFIX = "[Screenshot captured"
_COMPACTION_PREFIX = "[Conversation compacted"
_BACKGROUND_PREFIX = "Background terminal notifications arrived"


def _text_content(content: Any) -> str:
    """Extract text from a normal or vision-style message content value."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def is_screenshot_injection(msg: dict) -> bool:
    """Return whether *msg* is an agent-injected screenshot user message."""
    content = msg.get("content", "")
    if isinstance(content, list) and content:
        first_block = content[0]
        if isinstance(first_block, dict) and first_block.get("type") == "text":
            return first_block.get("text", "").startswith(_SCREENSHOT_PREFIX)
    return False


def _is_synthetic_user_message(msg: dict) -> bool:
    """Identify user-role messages generated internally by Kairos."""
    if msg.get("role") != "user":
        return False
    if is_screenshot_injection(msg):
        return True
    text = _text_content(msg.get("content", ""))
    return text.startswith((_COMPACTION_PREFIX, _BACKGROUND_PREFIX))


def _is_real_user_message(msg: dict) -> bool:
    return msg.get("role") == "user" and not _is_synthetic_user_message(msg)


def _extract_tool_call(tool_call: dict, index: int) -> tuple[str, str]:
    """Extract the ID and function name from a saved tool-call dictionary."""
    call_id = tool_call.get("id", "")
    function = tool_call.get("function", {})
    if isinstance(function, dict):
        name = function.get("name", "unknown")
    else:
        name = tool_call.get("name", "unknown")
    # A normal OpenAI tool call always has an ID. Keep a deterministic fallback
    # so a damaged save can still be repaired as far as possible.
    if not call_id:
        call_id = f"resume_missing_call_{index}"
    return call_id, name or "unknown"


def _make_synthetic_result(call_id: str, tool_name: str) -> dict:
    """Create a result for a tool call that was not completed before saving."""
    return {
        "tool_call_id": call_id,
        "role": "tool",
        "name": tool_name,
        "content": json.dumps({
            "success": False,
            "output": "",
            "error": "Tool was not executed — execution was interrupted.",
        }),
    }


def _repair_trailing_tool_chain(result: list[dict]) -> None:
    """Repair or remove a trailing tool chain in-place.

    A completed chain is left intact.  For a partial chain, existing results
    are rebuilt in the assistant tool-call order and missing results are
    synthesized.  If trailing tool messages have no matching assistant, they
    are removed rather than leaving an API-invalid orphaned chain.
    """
    if len(result) <= 1:
        return

    last_role = result[-1].get("role")
    if last_role == "assistant" and result[-1].get("tool_calls"):
        calls = result[-1]["tool_calls"]
        for index, tool_call in enumerate(calls):
            call_id, tool_name = _extract_tool_call(tool_call, index)
            result.append(_make_synthetic_result(call_id, tool_name))
        return

    if last_role != "tool":
        return

    chain_start = len(result) - 1
    while chain_start > 0 and result[chain_start - 1].get("role") == "tool":
        chain_start -= 1

    if chain_start == 0:
        del result[:]
        return

    assistant_index = chain_start - 1
    assistant = result[assistant_index]
    if assistant.get("role") != "assistant" or not assistant.get("tool_calls"):
        del result[chain_start:]
        return

    expected: list[tuple[str, str]] = []
    for index, tool_call in enumerate(assistant["tool_calls"]):
        expected.append(_extract_tool_call(tool_call, index))
    expected_ids = {call_id for call_id, _ in expected}

    # Keep only one result for each expected ID.  Rebuilding the chain also
    # fixes out-of-order results and removes any orphan/duplicate results.
    existing: dict[str, dict] = {}
    for message in result[chain_start:]:
        call_id = message.get("tool_call_id", "")
        if call_id in expected_ids and call_id not in existing:
            existing[call_id] = message

    repaired = [
        existing.get(call_id) or _make_synthetic_result(call_id, tool_name)
        for call_id, tool_name in expected
    ]
    result[chain_start:] = repaired


def sanitize_history_for_resume(
    history: list[dict],
) -> tuple[list[dict] | None, str, bool]:
    """Return a resumable history, final content, and mid-execution flag.

    The decision is made relative to the *latest real user request*, not the
    whole conversation.  This is important: an older completed assistant
    response must not hide a newer request whose tool-call chain was
    interrupted.

    Returns ``(history, last_agent_content, is_mid_execution)``.  A normal
    completed turn returns the history through its final assistant message.
    An unfinished turn returns repaired history with ``is_mid_execution=True``;
    the caller should then send a continuation user message.  ``(None, "",
    False)`` means the chat contains no real user request to resume.
    """
    if not history or len(history) <= 1:
        return None, "", False

    latest_user_index = None
    for index in range(len(history) - 1, 0, -1):
        if _is_real_user_message(history[index]):
            latest_user_index = index
            break

    if latest_user_index is None:
        return None, "", False

    # A clean assistant response after the latest request is the ordinary
    # resume seam.  Do not look before latest_user_index: those responses
    # belong to older requests and cannot make this request complete.
    for index in range(len(history) - 1, latest_user_index, -1):
        message = history[index]
        if message.get("role") == "assistant" and not message.get("tool_calls"):
            return history[: index + 1], message.get("content") or "", False

    # No final response exists for the latest request.  Remove agent-injected
    # screenshot messages at the end: they are visual context from the
    # unfinished turn, not a new user request, and leaving them after tool
    # results would make the continuation ordering ambiguous.
    result = list(history)
    while len(result) > 1 and is_screenshot_injection(result[-1]):
        result.pop()
    _repair_trailing_tool_chain(result)
    if len(result) <= 1:
        return None, "", False
    return result, "", True
