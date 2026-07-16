from __future__ import annotations

from pathlib import Path

from kairos.gateway.repository import GatewayRepository


def test_repository_round_trip(tmp_path: Path):
    repo = GatewayRepository(tmp_path)
    conversation = repo.create_conversation(str(tmp_path / "project"))
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    repo.replace_messages(conversation.id, history)
    loaded = repo.load_history(conversation.id)

    assert loaded == history
    assert repo.get_conversation(conversation.id).preview == "hello"
    assert [m.role for m in repo.list_messages(conversation.id)] == ["user", "assistant"]
    assert repo.delete_conversation(conversation.id) is True
    assert repo.get_conversation(conversation.id) is None


def test_run_public_and_private_serialization(tmp_path: Path):
    repo = GatewayRepository(tmp_path)
    conversation = repo.create_conversation(str(tmp_path))
    run = repo.create_run(conversation.id, "secret prompt", "data:image/png;base64,abc")
    public = run.to_dict()
    private = run.to_dict(include_request=True)
    assert "request_content" not in public
    assert private["request_content"] == "secret prompt"
    assert private["request_image_url"].startswith("data:image")


def test_events_have_monotonic_ids(tmp_path: Path):
    repo = GatewayRepository(tmp_path)
    conversation = repo.create_conversation(str(tmp_path))

    first = repo.append_event(conversation.id, "one", {})
    second = repo.append_event(conversation.id, "two", {"x": 1})

    assert first["event_id"] == 1
    assert second["event_id"] == 2
    assert [e["event"] for e in repo.list_events(conversation.id)] == ["one", "two"]
