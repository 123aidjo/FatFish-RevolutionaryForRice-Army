from fastapi.testclient import TestClient

from panel import create_app
from store import Store


def test_health_initializes_database(tmp_path):
    store = Store(tmp_path / "fishbot.db")
    with TestClient(create_app(store)) as client:
        result = client.get("/health")
        assert result.status_code == 200
        assert result.json() == {"service": "fishbot-v2-public", "status": "ok", "database": "ok"}
        assert store.path.is_file()


def test_health_reports_database_failure_without_leaking_path(tmp_path):
    store = Store(tmp_path / "fishbot.db")
    with TestClient(create_app(store)) as client:
        store.path.unlink()
        result = client.get("/health")
        assert result.status_code == 503
        assert result.json() == {
            "service": "fishbot-v2-public", "status": "unavailable", "database": "unavailable"
        }
        assert not store.path.exists()
