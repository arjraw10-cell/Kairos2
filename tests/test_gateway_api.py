import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kairos.gateway.manager import GatewayManager
from kairos.gateway.repository import GatewayRepository
from kairos.gateway.server import create_app

# The gateway refuses non-loopback Host headers (TestClient defaults to
# "testserver") unless a token is configured.
BASE_URL = "http://127.0.0.1:8765"
WS_URL = "ws://127.0.0.1:8765/api/v1/ws"

PROVIDER_CONFIG = {
    "default_setup": "fake",
    "providers": {"local": {"format": "completions", "base_url": "http://127.0.0.1:9/v1", "api_keys": ["k"]}},
    "setups": {"fake": {"provider": "local", "model": "fake-model"}},
}


@pytest.fixture(autouse=True)
def provider_config(tmp_path: Path, monkeypatch):
    """Real Agents read a temporary provider.json, not the repository's."""
    path = tmp_path / "provider.json"
    path.write_text(json.dumps(PROVIDER_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kairos.providers.config.config_path", lambda: path)


def test_health_and_readiness_are_public(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KAIROS_AUTH_TOKEN", "secret")
    from kairos.config import Config

    Config.reload()
    manager = GatewayManager(
        repository=GatewayRepository(tmp_path / "data"),
        max_concurrent_runs=1,
    )
    app = create_app(manager)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/v1/capabilities").status_code == 401
        assert client.get("/healthz/anything").status_code == 401
        assert client.get("/api/v1/capabilities", headers={"Authorization": "Bearer secret"}).status_code == 200
    monkeypatch.delenv("KAIROS_AUTH_TOKEN", raising=False)
    Config.reload()


def test_conversation_and_workspace_endpoints(tmp_path: Path):
    manager = GatewayManager(
        repository=GatewayRepository(tmp_path / "data"),
        default_workspace=None,
        max_concurrent_runs=1,
    )
    app = create_app(manager)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post(
            "/api/v1/conversations",
            json={"workspace": str(tmp_path / "project"), "title": "Test"},
        )
        assert response.status_code == 201
        conversation = response.json()
        conversation_id = conversation["id"]

        listed = client.get("/api/v1/conversations")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == conversation_id

        loaded = client.post(f"/api/v1/conversations/{conversation_id}/runtime/load")
        assert loaded.status_code == 200
        assert loaded.json()["conversation"]["runtime_loaded"] is True

        unloaded = client.post(f"/api/v1/conversations/{conversation_id}/runtime/unload")
        assert unloaded.status_code == 200

    assert not manager._runtimes


def test_conversation_without_workspace_gets_fresh_folder(tmp_path: Path, monkeypatch):
    from kairos import workspaces

    root = tmp_path / "kairos_workspace"
    monkeypatch.setattr(workspaces, "WORKSPACE_ROOT", root)
    manager = GatewayManager(
        repository=GatewayRepository(tmp_path / "data"),
        default_workspace=str(tmp_path / "old-default"),
        max_concurrent_runs=1,
    )
    app = create_app(manager)
    with TestClient(app, base_url=BASE_URL) as client:
        rest = client.post("/api/v1/conversations", json={"title": "REST"}).json()
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "command", "command": "conversation.create", "request_id": "r1", "payload": {}})
            reply = ws.receive_json()
            while reply.get("type") != "ack":
                reply = ws.receive_json()
        ws_conversation = reply["data"]["conversation"]
        explicit = client.post("/api/v1/conversations", json={"workspace": str(tmp_path / "project")}).json()

    rest_path = Path(rest["workspace_path"])
    ws_path = Path(ws_conversation["workspace_path"])
    assert rest_path.parent == root.resolve() and rest_path.is_dir()
    assert ws_path.parent == root.resolve() and ws_path.is_dir()
    assert rest_path != ws_path
    assert Path(explicit["workspace_path"]) == (tmp_path / "project").resolve()
