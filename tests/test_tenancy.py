from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from rag_aaup import api
from rag_aaup.config import Settings
from rag_aaup.tenancy import COOKIE


@pytest.fixture
def tenant_client(tmp_path: Path, monkeypatch):
    settings = Settings(
        _env_file=None,
        multi_tenant_enabled=True,
        workspace_root=tmp_path / "tenants",
        raw_dir=tmp_path / "legacy",
        database_path=tmp_path / "legacy.sqlite3",
        app_password=None,
        gemini_api_key=None,
        llm_provider="gemini",
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "generation_settings", lambda base: base)
    monkeypatch.setattr(
        "rag_aaup.indexing.service.encode_documents",
        lambda texts: np.asarray([[1, 0]] * len(texts), dtype=np.float32),
    )
    monkeypatch.setattr("rag_aaup.retrieval.service.encode_query", lambda _: np.asarray([1, 0]))
    with TestClient(api.app) as client:
        yield client


def register(client, name):
    response = client.post(
        "/api/auth/register", json={"username": name, "password": "Test-password-2026!", "company_name": name}
    )
    assert response.status_code == 201
    return {"Cookie": f"{COOKIE}={response.cookies.get(COOKIE)}"}, response.json()


def upload(client, headers, name, text):
    response = client.post(
        "/api/documents", headers=headers, files={"files": (name, text.encode(), "text/plain")}
    )
    assert response.status_code == 200
    return response.json()


def test_unauthenticated_and_forged_sessions_cannot_read_documents(tenant_client):
    client = tenant_client
    assert client.get("/").status_code == 200
    assert client.get("/static/workspace.js").status_code == 200
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/workspace", headers={"Cookie": f"{COOKIE}=forged"}).status_code == 401


def test_company_files_retrieval_and_download_are_isolated(tenant_client):
    client = tenant_client
    first, a = register(client, "alpha-owner")
    second, b = register(client, "beta-owner")
    assert a["tenant_id"] != b["tenant_id"]
    upload(client, first, "policy.txt", "ALPHA_ONLY_62814 confidential policy")
    upload(client, first, "extra.txt", "Another alpha document")
    upload(client, second, "policy.txt", "BETA_ONLY_90725 private policy")
    assert len(client.get("/api/documents", headers=first).json()) == 2
    assert len(client.get("/api/documents", headers=second).json()) == 1
    first_files = client.get("/api/documents", headers=first).json()
    extra = next(item for item in first_files if item["source"] == "extra.txt")
    assert client.get(extra["url"], headers=second).status_code == 404
    assert (
        client.post(
            "/api/ask",
            headers=second,
            json={"question": "policy", "mode": "sources", "document_id": extra["document_id"]},
        ).json()["sources"]
        == []
    )
    beta_file = client.get("/api/documents", headers=second).json()[0]
    assert "BETA_ONLY" in client.get(beta_file["url"], headers=second).text
    results = client.post(
        "/api/ask",
        headers={**second, "X-Tenant-ID": a["tenant_id"]},
        json={"question": "ALPHA_ONLY_62814", "mode": "sources", "tenant_id": a["tenant_id"]},
    ).json()
    assert all("ALPHA_ONLY" not in item["text"] for item in results["sources"])
    assert all(
        "BETA_ONLY" not in item["text"]
        for item in client.post(
            "/api/ask", headers=first, json={"question": "policy", "mode": "sources"}
        ).json()["sources"]
    )


def test_duplicate_upload_is_skipped_and_clear_only_affects_owner(tenant_client):
    client = tenant_client
    first, _ = register(client, "first-owner")
    second, _ = register(client, "second-owner")
    upload(client, first, "one.txt", "FIRST unique company policy")
    skipped = upload(client, first, "copy.txt", "FIRST unique company policy")
    assert skipped["skipped_duplicates"] == ["copy.txt"]
    upload(client, second, "second.txt", "SECOND unique company policy")
    assert (
        client.request("DELETE", "/api/documents", headers=first, json={"confirmation": "wrong"}).status_code
        == 422
    )
    response = client.request("DELETE", "/api/documents", headers=first, json={"confirmation": "DELETE"})
    assert response.status_code == 200
    assert response.json()["quarantined"] == ["one.txt"]
    assert client.get("/api/documents", headers=first).json() == []
    assert len(client.get("/api/documents", headers=second).json()) == 1


def test_credentials_are_hashed_logout_revokes_session_and_origin_is_checked(tenant_client):
    client = tenant_client
    headers, principal = register(client, "secure-owner")
    accounts = api.app.state.registry.accounts
    raw = accounts.path.read_bytes()
    assert b"Test-password-2026!" not in raw
    assert headers["Cookie"].split("=", 1)[1].encode() not in raw
    denied = client.post("/api/reindex", headers={**headers, "Origin": "https://attacker.invalid"})
    assert denied.status_code == 403
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    login = client.post(
        "/api/auth/login", json={"username": principal["username"], "password": "wrong-password"}
    )
    assert login.status_code == 401
    login = client.post(
        "/api/auth/login", json={"username": principal["username"], "password": "Test-password-2026!"}
    )
    assert login.status_code == 200
    assert "httponly" in login.headers["set-cookie"].lower()


def test_clear_stops_managed_reference_reinstallation(tenant_client, monkeypatch):
    client = tenant_client
    headers, principal = register(client, "managed-owner")
    registry = api.app.state.registry
    registry.runtime(principal["tenant_id"])
    registry.accounts.set_academic(principal["tenant_id"], True)
    monkeypatch.setattr(registry.references, "sync", dict)

    def install(directory):
        (directory / "managed.txt").write_text("Managed source", encoding="utf-8")

    monkeypatch.setattr(registry.references, "install", install)
    registry.sync_all()
    assert len(client.get("/api/documents", headers=headers).json()) == 1
    client.request("DELETE", "/api/documents", headers=headers, json={"confirmation": "DELETE"})
    registry.sync_all()
    assert client.get("/api/documents", headers=headers).json() == []
