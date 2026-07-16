from kairos.gateway.history import sanitize_for_resume, visible_history


def test_clean_history_is_resumable_without_continuation():
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]
    sanitized, needs_continue = sanitize_for_resume(history)
    assert sanitized == history
    assert needs_continue is False
    assert visible_history(history) == history[1:]


def test_real_user_without_assistant_is_preserved_for_continue():
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "last request"},
    ]
    sanitized, needs_continue = sanitize_for_resume(history, prefer_incomplete=True)
    assert sanitized == history
    assert needs_continue is True


def test_dirty_suffix_load_keeps_latest_clean_assistant_boundary():
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_2", "function": {"name": "read", "arguments": "{}"}}],
        },
    ]
    sanitized, needs_continue = sanitize_for_resume(history)
    assert sanitized == history[:3]
    assert needs_continue is False


def test_incomplete_tool_call_gets_valid_synthetic_result():
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
    ]
    sanitized, needs_continue = sanitize_for_resume(history, prefer_incomplete=True)
    assert needs_continue is True
    assert sanitized[-1]["role"] == "tool"
    assert sanitized[-1]["tool_call_id"] == "call_1"
