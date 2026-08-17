"""The memory, project, and knowledge HTTP surface.

Authorisation and the destructive paths get the most attention here, because
these are the endpoints that can erase things and the ones an attacker reaching
the port would reach first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def make_memory(client: TestClient, content: str, **kwargs) -> dict:
    payload = {"content": content, "type": kwargs.pop("type", "USER_PREFERENCE")}
    payload.update(kwargs)
    response = client.post("/api/memories", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ── memory CRUD ──────────────────────────────────────────────────────────────


def test_create_and_list(client: TestClient) -> None:
    make_memory(client, "The user prefers dark mode", subject="theme")
    body = client.get("/api/memories").json()
    assert body["total"] == 1
    assert body["memories"][0]["confidence_band"] == "CERTAIN"


def test_create_reports_what_actually_happened(client: TestClient) -> None:
    make_memory(client, "Prefers dark mode", subject="theme")
    second = client.post(
        "/api/memories",
        json={"content": "Prefers dark mode", "type": "USER_PREFERENCE",
              "subject": "theme"},
    ).json()
    assert second["action"] == "merged"
    assert second["detail"]


def test_contradiction_is_reported_as_superseded(client: TestClient) -> None:
    make_memory(client, "Works at night", subject="hours")
    second = client.post(
        "/api/memories",
        json={"content": "Works in the morning", "type": "USER_PREFERENCE",
              "subject": "hours"},
    ).json()
    assert second["action"] == "superseded"


def test_get_includes_history_and_relations(client: TestClient) -> None:
    created = make_memory(client, "Something worth editing", subject="edit me")
    client.patch(f"/api/memories/{created['id']}", json={"content": "Edited"})

    detail = client.get(f"/api/memories/{created['id']}").json()
    assert detail["content"] == "Edited"
    assert {h["kind"] for h in detail["history"]} >= {"CREATED", "CORRECTED"}


def test_patch_with_no_fields_is_rejected(client: TestClient) -> None:
    created = make_memory(client, "Unchanged", subject="x")
    assert client.patch(f"/api/memories/{created['id']}", json={}).status_code == 400


def test_missing_memory_is_404(client: TestClient) -> None:
    assert client.get("/api/memories/mem_nope").status_code == 404


def test_archive_restore_delete(client: TestClient) -> None:
    created = make_memory(client, "Transient", subject="t")
    memory_id = created["id"]

    assert client.post(f"/api/memories/{memory_id}/archive").json()["status"] == "ARCHIVED"
    assert client.post(f"/api/memories/{memory_id}/restore").json()["status"] == "ACTIVE"
    assert client.delete(f"/api/memories/{memory_id}").status_code == 204
    assert client.get(f"/api/memories/{memory_id}").json()["content"] == ""


def test_search_returns_inspectable_scores(client: TestClient) -> None:
    make_memory(client, "Avoid PyAutoGUI; it is unmaintained",
                type="LESSON_LEARNED", subject="automation library")
    body = client.get("/api/memories/search", params={"q": "PyAutoGUI"}).json()
    assert body["results"]
    assert set(body["results"][0]["score"]) >= {"semantic", "keyword", "total"}
    assert body["semantic"] is False, "lexical fallback must be declared"


def test_stats_declare_the_capture_mode(client: TestClient) -> None:
    body = client.get("/api/memories/stats").json()
    assert body["capture_mode"] in {"ask", "auto", "off"}
    assert body["semantic_search"] is False


# ── secrets and destructive paths ────────────────────────────────────────────


def test_secret_is_refused_with_422(client: TestClient) -> None:
    response = client.post(
        "/api/memories",
        json={"content": "my password is Xk8$mQ2vL9pR7z", "type": "USER_FACT"},
    )
    assert response.status_code == 422
    assert "Xk8" not in response.text, "the refusal must not echo the secret"


def test_secret_can_be_stored_with_an_explicit_override(client: TestClient) -> None:
    response = client.post(
        "/api/memories",
        json={"content": "my password is Xk8$mQ2vL9pR7z", "type": "USER_FACT",
              "allow_sensitive": True},
    )
    assert response.status_code == 201


def test_bulk_forget_refuses_an_empty_scope(client: TestClient) -> None:
    make_memory(client, "Keep me", subject="keep")
    assert client.post("/api/memories/forget", json={}).status_code == 400
    assert client.get("/api/memories").json()["total"] == 1


def test_bulk_forget_all_requires_the_flag(client: TestClient) -> None:
    make_memory(client, "Doomed", subject="d")
    body = client.post("/api/memories/forget", json={"all_memories": True}).json()
    assert body["forgotten"] == 1


# ── export / import ──────────────────────────────────────────────────────────


def test_export_declares_what_it_omits(client: TestClient) -> None:
    make_memory(client, "Exportable", subject="e")
    archive = client.get("/api/memories/export/archive").json()
    assert archive["format"] == "jarvis.memory"
    assert archive["excludes"] == ["embeddings"]
    assert archive["count"] == 1


def test_export_markdown_is_human_readable(client: TestClient) -> None:
    make_memory(client, "The user prefers dark mode", subject="theme")
    text = client.get(
        "/api/memories/export/archive", params={"format": "markdown"}
    ).text
    assert "# JARVIS memory export" in text
    assert "dark mode" in text
    # Bands, not floats.
    assert "confidence certain" in text


def test_import_round_trips(client: TestClient) -> None:
    make_memory(client, "Round trip me", subject="rt")
    archive = client.get("/api/memories/export/archive").json()

    client.post("/api/memories/forget", json={"all_memories": True, "hard": True})
    assert client.get("/api/memories").json()["total"] == 0

    report = client.post("/api/memories/import", json=archive).json()
    assert report["created"] == 1
    assert client.get("/api/memories").json()["total"] == 1


def test_import_rejects_a_foreign_format(client: TestClient) -> None:
    response = client.post("/api/memories/import", json={"format": "something-else"})
    assert response.status_code == 422


def test_import_runs_the_secret_guard(client: TestClient) -> None:
    """An archive is untrusted input like any other file."""
    archive = {
        "format": "jarvis.memory",
        "version": 1,
        "memories": [
            {"content": "my password is Xk8$mQ2vL9pR7z", "type": "USER_FACT",
             "source": "USER", "confidence": 1.0}
        ],
    }
    report = client.post("/api/memories/import", json=archive).json()
    assert report["refused"] == 1
    assert report["created"] == 0


# ── projects ─────────────────────────────────────────────────────────────────


def test_project_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/projects", json={"name": "Project X", "goals": ["Ship a slice"]}
    )
    assert created.status_code == 201
    project = created.json()
    assert project["key"] == "project-x"

    client.patch(
        f"/api/projects/{project['id']}",
        json={"current_state": "Blocked on streaming"},
    )
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["current_state"] == "Blocked on streaming"
    assert detail["stats"]["memories"] == 0


def test_duplicate_project_key_is_409(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Project X"})
    assert client.post("/api/projects", json={"name": "project x"}).status_code == 409


def test_project_memories_are_counted(client: TestClient) -> None:
    project = client.post("/api/projects", json={"name": "Nebula"}).json()
    make_memory(client, "Nebula uses canvas rendering",
                type="PROJECT_DECISION", subject="rendering",
                project_id=project["id"])

    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["stats"]["memories"] == 1
    assert detail["decisions"]


# ── knowledge ────────────────────────────────────────────────────────────────


MD = b"# Architecture\n\nVectors live in SQLite as float32 blobs, searched by brute force.\n"


def test_upload_ingest_and_search(client: TestClient) -> None:
    response = client.post(
        "/api/knowledge/ingest/upload",
        files={"file": ("arch.md", MD, "text/markdown")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["chunks"] >= 1

    hits = client.get("/api/knowledge/search", params={"q": "vectors"}).json()
    assert hits["results"]
    assert hits["results"][0]["citation"]
    assert hits["results"][0]["provenance"]["kind"] == "UPLOAD"


def test_document_listing_reports_real_status(client: TestClient) -> None:
    client.post(
        "/api/knowledge/ingest/upload",
        files={"file": ("arch.md", MD, "text/markdown")},
    )
    body = client.get("/api/knowledge/documents").json()
    assert body["documents"][0]["status"] == "INDEXED"
    assert body["stats"]["chunks"] >= 1


def test_document_detail_exposes_chunks(client: TestClient) -> None:
    created = client.post(
        "/api/knowledge/ingest/upload",
        files={"file": ("arch.md", MD, "text/markdown")},
    ).json()
    detail = client.get(f"/api/knowledge/documents/{created['document_id']}").json()
    assert detail["chunks"]
    assert detail["tainted"] is True


def test_document_delete(client: TestClient) -> None:
    created = client.post(
        "/api/knowledge/ingest/upload",
        files={"file": ("arch.md", MD, "text/markdown")},
    ).json()
    assert client.delete(
        f"/api/knowledge/documents/{created['document_id']}"
    ).status_code == 204
    assert client.get("/api/knowledge/documents").json()["documents"] == []


def test_unsupported_upload_is_refused_by_name(client: TestClient) -> None:
    response = client.post(
        "/api/knowledge/ingest/upload",
        files={"file": ("report.docx", b"PK\x03\x04binary", "application/msword")},
    )
    assert response.status_code == 422
    assert "docx" in response.text


def test_ingest_from_path_refused_without_roots(client: TestClient) -> None:
    """The safe default: no configured roots means nothing on disk is readable."""
    response = client.post(
        "/api/knowledge/ingest/path", json={"path": "/etc/passwd"}
    )
    assert response.status_code == 422


def test_sources_list_obsidian_as_implemented_but_unconnected(
    client: TestClient,
) -> None:
    """Phase 2.5 flipped ``implemented``; ``connected`` still has to be earned
    by a reachable vault, and no vault is configured in a test."""
    body = client.get("/api/knowledge/sources").json()
    sources = {s["key"]: s for s in body["sources"]}
    assert sources["obsidian"]["implemented"] is True
    assert sources["obsidian"]["connected"] is False
    assert sources["internal"]["implemented"] is True
    assert body["semantic_search"] is False


def test_sources_report_unsupported_formats(client: TestClient) -> None:
    formats = {f["key"]: f for f in client.get("/api/knowledge/sources").json()["formats"]}
    assert formats["markdown"]["available"] is True
    assert formats["docx"]["available"] is False


# ── authentication ───────────────────────────────────────────────────────────


@pytest.fixture
def authed_client(core, monkeypatch):
    from jarvis.api.app import create_app
    from jarvis.config import Settings, reset_config_caches

    monkeypatch.setenv("JARVIS_API_TOKEN", "test-token-abcdefghijklmnop")
    reset_config_caches()
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        require_auth=True,
        log_level="CRITICAL",
    )
    core.settings = settings
    with TestClient(create_app(settings, core=core)) as c:
        yield c


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/memories"),
        ("get", "/api/memories/search?q=x"),
        ("get", "/api/memories/stats"),
        ("post", "/api/memories/forget"),
        ("get", "/api/memories/export/archive"),
        ("get", "/api/projects"),
        ("get", "/api/knowledge/documents"),
        ("get", "/api/knowledge/search?q=x"),
        ("get", "/api/knowledge/sources"),
        ("post", "/api/knowledge/ingest/path"),
    ],
)
def test_every_new_endpoint_requires_a_token(
    authed_client: TestClient, method: str, path: str
) -> None:
    # GET must not be handed a body — httpx rejects `json=` on get(), which
    # would fail the test for the wrong reason and hide a genuine gap.
    response = (
        authed_client.post(path, json={})
        if method == "post"
        else authed_client.get(path)
    )
    assert response.status_code == 401, f"{method.upper()} {path} was not protected"


def test_memory_endpoints_never_leak_a_credential(client: TestClient) -> None:
    make_memory(client, "Ordinary memory", subject="ordinary")
    for path in ("/api/memories", "/api/memories/stats", "/api/knowledge/sources"):
        raw = client.get(path).text.lower()
        for marker in ("api_key", "apikey", "password", "bearer "):
            assert marker not in raw, f"{path} leaked {marker}"
