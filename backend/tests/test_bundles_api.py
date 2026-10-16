"""
Phase 5 integration tests: HTTP endpoints for bundle management.

Uses FastAPI's TestClient to exercise the full pipeline over HTTP.

Run:
    cd D:\\AI\\peldrun\\backend
    python -m pytest tests/test_bundles_api.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omweb.routers.store import bundles as bundles_module
from omweb.tools.bundles.manager import BundleManager
from tests.fixtures import bundle_factory as bf


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """
    Isolated FastAPI app with the bundles router, backed by a tmp store.
    """
    # Force BundleManager to use the tmp path
    monkeypatch.setattr(
        bundles_module, "_manager", BundleManager(tmp_path / "store_tools")
    )

    app = FastAPI()
    app.include_router(bundles_module.router, prefix="/api/store/tools")

    return TestClient(app)


def _upload_bundle(client: TestClient, payload: bytes, name: str = "test.zip"):
    return client.post(
        "/api/store/tools/upload-bundle",
        files={"file": (name, payload, "application/zip")},
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class TestUploadBundle:
    def test_upload_valid_bundle(self, client: TestClient):
        payload = bf.make_valid_bundle()
        response = _upload_bundle(client, payload)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "success"
        assert data["bundle"]["id"] == "test_tool"
        assert len(data["bundle"]["hash"]) == 16

    def test_upload_rejects_non_zip(self, client: TestClient):
        response = client.post(
            "/api/store/tools/upload-bundle",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "BUNDLE_WRONG_FILE_TYPE"

    def test_upload_rejects_empty(self, client: TestClient):
        response = client.post(
            "/api/store/tools/upload-bundle",
            files={"file": ("empty.zip", b"", "application/zip")},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "BUNDLE_EMPTY_UPLOAD"

    def test_upload_rejects_traversal(self, client: TestClient):
        payload = bf.make_bundle_with_traversal()
        response = _upload_bundle(client, payload, "evil.zip")
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "BUNDLE_VALIDATION_FAILED"
        assert any("traversal" in e for e in detail["details"]["errors"])

    def test_upload_rejects_missing_manifest(self, client: TestClient):
        payload = bf.make_bundle_missing_manifest()
        response = _upload_bundle(client, payload, "broken.zip")
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestListBundles:
    def test_empty_list(self, client: TestClient):
        response = client.get("/api/store/tools/bundles")
        assert response.status_code == 200
        data = response.json()
        assert data["bundles"] == []
        assert data["total"] == 0

    def test_list_after_upload(self, client: TestClient):
        _upload_bundle(client, bf.make_valid_bundle())
        response = client.get("/api/store/tools/bundles")
        data = response.json()
        assert data["total"] == 1
        assert data["bundles"][0]["id"] == "test_tool"


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

class TestBundleDetail:
    def test_get_existing_bundle(self, client: TestClient):
        _upload_bundle(client, bf.make_valid_bundle())
        response = client.get("/api/store/tools/bundles/test_tool")
        assert response.status_code == 200
        assert response.json()["bundle"]["id"] == "test_tool"

    def test_get_missing_bundle(self, client: TestClient):
        response = client.get("/api/store/tools/bundles/nope")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "BUNDLE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

class TestActivateVersion:
    def test_activate_previous_version(self, client: TestClient):
        # Install two versions
        p1 = bf.make_valid_bundle(manifest_overrides={"version": "1.0.0"})
        p2 = bf.make_valid_bundle(manifest_overrides={"version": "2.0.0"})
        _upload_bundle(client, p1, "v1.zip")
        _upload_bundle(client, p2, "v2.zip")

        # Activate v1
        response = client.post(
            "/api/store/tools/bundles/test_tool/activate",
            json={"version": "1.0.0"},
        )
        assert response.status_code == 200
        assert response.json()["active_version"] == "1.0.0"

        # Verify via detail endpoint
        detail = client.get("/api/store/tools/bundles/test_tool").json()
        assert detail["bundle"]["active_version"] == "1.0.0"

    def test_activate_missing_version(self, client: TestClient):
        _upload_bundle(client, bf.make_valid_bundle())
        response = client.post(
            "/api/store/tools/bundles/test_tool/activate",
            json={"version": "9.9.9"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestBundleHealth:
    def test_health_healthy(self, client: TestClient):
        _upload_bundle(client, bf.make_valid_bundle())
        response = client.get("/api/store/tools/bundles/test_tool/health")
        assert response.status_code == 200
        health = response.json()["health"]
        assert health["status"] == "healthy"
        assert all(c["passed"] for c in health["checks"])

    def test_health_missing_bundle(self, client: TestClient):
        response = client.get("/api/store/tools/bundles/nope/health")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDeleteBundle:
    def test_delete_single_version(self, client: TestClient):
        _upload_bundle(client, bf.make_valid_bundle())
        response = client.delete("/api/store/tools/bundles/test_tool")
        assert response.status_code == 200

        # Verify gone
        assert client.get("/api/store/tools/bundles/test_tool").status_code == 404

    def test_delete_missing(self, client: TestClient):
        response = client.delete("/api/store/tools/bundles/nope")
        assert response.status_code == 404

    def test_delete_multi_version_refused(self, client: TestClient):
        p1 = bf.make_valid_bundle(manifest_overrides={"version": "1.0.0"})
        p2 = bf.make_valid_bundle(manifest_overrides={"version": "2.0.0"})
        _upload_bundle(client, p1, "v1.zip")
        _upload_bundle(client, p2, "v2.zip")

        # Without force: refused
        response = client.delete("/api/store/tools/bundles/test_tool")
        assert response.status_code == 400

        # With force: succeeds
        response = client.delete("/api/store/tools/bundles/test_tool?force=true")
        assert response.status_code == 200
