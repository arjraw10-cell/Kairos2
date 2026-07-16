from pathlib import Path

from fastapi.testclient import TestClient

from kairos.gateway.manager import GatewayManager
from kairos.gateway.repository import GatewayRepository
from kairos.gateway.server import create_app


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
    with TestClient(app) as client:
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
