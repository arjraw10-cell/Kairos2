"""History repair helpers shared by gateway loading and continuation."""
from __future__ import annotations

import copy
import json
from typing import Any


def is_screenshot_injection(message: dict[str, Any]) -> bool:
    content = message.get("content", "")
    if isinstance(content, list) and content:
        first = content[0]
        return (
            isinstance(first, dict)
            and first.get("type") == "text"
            and str(first.get("text", "")).startswith("[Screenshot captured")
        )
    return False


def _tool_call_info(tool_call: dict[str, Any], index: int) -> tuple[str, str, dict[str, Any]]:
    function = tool_call.get("function") or {}
    name = str(function.get("name") or tool_call.get("name") or "unknown")
    call_id = str(tool_call.get("id") or "")
    if not call_id:
        call_id = f"interrupted_call_{index}"
    normalized = copy.deepcopy(tool_call)
    normalized["id"] = call_id
    normalized.setdefault("type", "function")
    normalized["function"] = copy.deepcopy(function)
    normalized["function"].setdefault("name", name)
    normalized["function"].setdefault("arguments", "{}")
    return call_id, name, normalized


def _synthetic_result(tool_call_id: str, name: str) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "name": name,
        "content": json.dumps(
            {
                "success": False,
                "output": "",
                "error": "Tool was not executed — execution was interrupted.",
            }
        ),
    }


def _repair_incomplete_suffix(
    history: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, bool] | None:
    """Repair an interrupted suffix, or return ``None`` if it is complete."""
    result = copy.deepcopy(history)
    while len(result) > 1 and is_screenshot_injection(result[-1]):
        result.pop()
    if len(result) <= 1:
        return None, False

    last = result[-1]
    if last.get("role") == "assistant" and last.get("tool_calls"):
        normalized_calls: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, call in enumerate(last["tool_calls"]):
            call_id, name, normalized = _tool_call_info(call, index)
            if call_id in seen:
                call_id = f"interrupted_call_{index}"
                normalized["id"] = call_id
            seen.add(call_id)
            normalized_calls.append(normalized)
        assistant = copy.deepcopy(last)
        assistant["tool_calls"] = normalized_calls
        result[-1] = assistant
        for call in normalized_calls:
            call_id, name, _ = _tool_call_info(call, 0)
            result.append(_synthetic_result(call_id, name))
        return result, True

    if last.get("role") == "tool":
        index = len(result) - 1
        while index > 0 and result[index].get("role") == "tool":
            index -= 1
        if index > 0 and result[index].get("role") == "assistant" and result[index].get("tool_calls"):
            assistant = copy.deepcopy(result[index])
            normalized_calls: list[dict[str, Any]] = []
            call_names: dict[str, str] = {}
            seen: set[str] = set()
            for call_index, call in enumerate(assistant["tool_calls"]):
                call_id, name, normalized = _tool_call_info(call, call_index)
                if call_id in seen:
                    call_id = f"interrupted_call_{call_index}"
                    normalized["id"] = call_id
                seen.add(call_id)
                normalized_calls.append(normalized)
                call_names[call_id] = name
            assistant["tool_calls"] = normalized_calls

            # Keep the first valid result for each expected call; discard
            # orphan/duplicate results and synthesize every missing result.
            valid_results: dict[str, dict[str, Any]] = {}
            for message in result[index + 1 :]:
                call_id = str(message.get("tool_call_id") or "")
                if call_id in call_names and call_id not in valid_results:
                    valid_results[call_id] = copy.deepcopy(message)
            result = result[:index] + [assistant]
            for call_id, name in call_names.items():
                result.append(valid_results.get(call_id) or _synthetic_result(call_id, name))
            return result, True

        # Tool results without a matching assistant tool-call message are an
        # orphan suffix and cannot be sent to the API.
        while len(result) > 1 and result[-1].get("role") == "tool":
            result.pop()
        return (result if len(result) > 1 else None), True

    # A real user message without an assistant response is a valid interrupted
    # boundary. Preserve it so an explicit continuation can pick up the
    # request instead of losing the user's last input.
    if last.get("role") == "user" and not is_screenshot_injection(last):
        return result, True

    return None


def sanitize_for_resume(
    history: list[dict[str, Any]], prefer_incomplete: bool = False
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Return an API-valid history and whether continuation is needed.

    Normal loading prefers the latest clean assistant response and discards an
    incomplete suffix. ``prefer_incomplete=True`` is used by an explicit
    continue command to repair the latest tool chain instead, preserving the
    interrupted Agent intent with synthetic error results.
    """
    if not history or len(history) <= 1:
        return None, False

    if prefer_incomplete:
        repaired = _repair_incomplete_suffix(history)
        if repaired is not None:
            return repaired

    # Prefer the latest completed assistant response. This makes a normal
    # runtime load safe even when a prior process left a dirty suffix. Keep
    # walking past users and incomplete tool chains: the clean assistant
    # response is the last API-safe boundary, so everything after it must be
    # discarded for an ordinary load.
    index = len(history) - 1
    while index > 0:
        message = history[index]
        role = message.get("role", "")
        if role == "assistant" and not message.get("tool_calls"):
            return copy.deepcopy(history[: index + 1]), False
        index -= 1

    repaired = _repair_incomplete_suffix(history)
    if repaired is not None:
        return repaired
    return None, False


def visible_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return user/assistant messages safe for a client display."""
    result = []
    for message in history:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        if is_screenshot_injection(message) or message.get("tool_calls"):
            continue
        result.append({"role": role, "content": message.get("content") or ""})
    return result
