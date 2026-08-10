"""The Obsidian HTTP surface, and the full workflow through it (§27 INTEGRATION).

These drive the same paths the UI does, which is the point: the panel is not
allowed to know anything the API does not report, so whatever these tests can
see is what a user can see.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "APIVault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "Notes").mkdir()
    (root / "Notes" / "Rust.md").write_text(
        "---\ntags: [languages]\n---\n\n# Rust\n\nOwnership and borrowing.\n",
        encoding="utf-8",
    )
    (root / "Index.md").write_text("# Index\n\nSee [[Rust]].\n", encoding="utf-8")
    return root


def _connect(client: TestClient, vault: Path, **kwargs) -> dict:
    body = {"vault_path": str(vault), "vault_name": "APIVault", **kwargs}
    response = client.post("/api/obsidian/connect", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ── discovery and connection ─────────────────────────────────────────────────


def test_discover_never_invents_a_path(client: TestClient) -> None:
    """§5: when nothing is found, the answer is a configuration field, not a
    guess. Whatever this machine has, every reported vault must be a real
    directory."""
    body = client.get("/api/obsidian/discover").json()
    assert "vaults" in body and "needs_manual_configuration" in body
    for vault in body["vaults"]:
        assert Path(vault["path"]).is_dir()
    if not body["vaults"]:
        assert body["needs_manual_configuration"] is True
        assert body["notes"]


def test_status_is_disconnected_before_anything_is_configured(
    client: TestClient,
) -> None:
    body = client.get("/api/obsidian/status").json()
    assert body["implemented"] is True
    assert body["configured"] is False
    assert body["connected"] is False
    assert body["state"] == "DISCONNECTED"


def test_connect_then_status_reports_connected(
    client: TestClient, vault: Path
) -> None:
    result = _connect(client, vault)
    assert result["connected"] is True
    assert result["notes"] == 2

    body = client.get("/api/obsidian/status").json()
    assert body["state"] == "CONNECTED"
    assert body["vault"]["name"] == "APIVault"
    assert body["config"]["vault_path"].startswith("…/")
    assert "READ" in body["capabilities"]


def test_connecting_to_a_nonexistent_vault_is_an_error_not_a_state(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/api/obsidian/connect", json={"vault_path": str(tmp_path / "missing")}
    )
    assert response.status_code == 400
    assert "does not exist" in response.json()["error"]["message"]
    assert client.get("/api/obsidian/status").json()["connected"] is False


def test_status_goes_to_error_when_the_vault_disappears(
    client: TestClient, vault: Path
) -> None:
    """§7: do not fake any status. ``connected`` is walked, not remembered."""
    import shutil

    _connect(client, vault)
    assert client.get("/api/obsidian/status").json()["state"] == "CONNECTED"

    shutil.rmtree(vault)
    body = client.get("/api/obsidian/status").json()
    assert body["state"] == "ERROR"
    assert body["connected"] is False
    assert "does not exist" in body["detail"]


def test_test_connection_reports_live_state(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    assert client.post("/api/obsidian/test").json()["notes"] == 2


def test_disconnect_leaves_the_vault_alone(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    client.post("/api/obsidian/disconnect")
    assert (vault / "Index.md").exists()
    assert client.get("/api/obsidian/status").json()["connected"] is False


# ── read and search ──────────────────────────────────────────────────────────


def test_list_notes_and_folders(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    notes = client.get("/api/obsidian/notes").json()
    assert {n["path"] for n in notes["notes"]} == {"Index.md", "Notes/Rust.md"}
    assert client.get("/api/obsidian/folders").json()["folders"] == ["Notes"]


def test_read_note_returns_markdown_and_a_trust_label(
    client: TestClient, vault: Path
) -> None:
    _connect(client, vault)
    body = client.get("/api/obsidian/note", params={"path": "Notes/Rust.md"}).json()
    assert body["content"] == (vault / "Notes" / "Rust.md").read_text()
    assert "languages" in body["tags"]
    assert "untrusted data" in body["trust"]


def test_read_outside_the_vault_is_refused(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    response = client.get(
        "/api/obsidian/note", params={"path": "../../../etc/passwd"}
    )
    assert response.status_code in {400, 404}


def test_search_over_http(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    body = client.get("/api/obsidian/search", params={"q": "ownership"}).json()
    assert body["results"][0]["path"] == "Notes/Rust.md"
    assert body["results"][0]["excerpt"]


def test_links_and_backlinks_over_http(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    body = client.get("/api/obsidian/links", params={"path": "Notes/Rust.md"}).json()
    assert body["backlinks"] == ["Index.md"]


def test_operations_without_a_vault_are_a_clear_404(client: TestClient) -> None:
    """§22: no vault is a state JARVIS handles, not a crash."""
    response = client.get("/api/obsidian/notes")
    assert response.status_code == 404
    # JarvisError's envelope, not FastAPI's — the same shape every other
    # structured error in the system uses.
    assert "Connect one" in response.json()["error"]["message"]


# ── write ────────────────────────────────────────────────────────────────────


def test_create_is_refused_when_writes_are_off(
    client: TestClient, vault: Path
) -> None:
    _connect(client, vault, allow_writes=False)
    response = client.post(
        "/api/obsidian/notes", json={"title": "New", "content": "body"}
    )
    assert response.status_code == 403
    assert "switched off" in response.json()["error"]["message"]
    assert not (vault / "New.md").exists()


def test_create_asks_first_then_writes(client: TestClient, vault: Path) -> None:
    """The whole approval round trip: a write is proposed, a confirmation
    comes back, the user approves, and only then does a file appear."""
    _connect(client, vault, allow_writes=True)

    first = client.post(
        "/api/obsidian/notes",
        json={"title": "Decisions", "content": "# Decisions\n\nWhy we chose X.",
              "path": "Notes/Decisions.md"},
    ).json()
    assert first["status"] == "needs_confirmation"
    assert not (vault / "Notes" / "Decisions.md").exists()

    client.post(
        f"/api/confirmations/{first['confirmation_id']}/decide",
        json={"approved": True},
    )

    second = client.post(
        "/api/obsidian/notes",
        json={"title": "Decisions", "content": "# Decisions\n\nWhy we chose X.",
              "path": "Notes/Decisions.md"},
    ).json()
    assert second["path"] == "Notes/Decisions.md"
    assert (vault / "Notes" / "Decisions.md").is_file()
    # Written and indexed in one step, so it is findable immediately.
    assert second["indexed"]["chunks"] >= 1


def test_an_approval_is_single_use(client: TestClient, vault: Path) -> None:
    _connect(client, vault, allow_writes=True)
    payload = {"title": "Once", "content": "body", "path": "Notes/Once.md"}

    first = client.post("/api/obsidian/notes", json=payload).json()
    client.post(
        f"/api/confirmations/{first['confirmation_id']}/decide",
        json={"approved": True},
    )
    client.post("/api/obsidian/notes", json=payload)
    (vault / "Notes" / "Once.md").unlink()

    # The same request again must ask again — an approval authorises one act.
    again = client.post("/api/obsidian/notes", json=payload).json()
    assert again.get("status") == "needs_confirmation"


def test_append_does_not_destroy(client: TestClient, vault: Path) -> None:
    _connect(client, vault, allow_writes=True)
    response = client.patch(
        "/api/obsidian/note", params={"path": "Index.md"},
        json={"content": "Appended by JARVIS.", "mode": "append"},
    ).json()
    client.post(
        f"/api/confirmations/{response['confirmation_id']}/decide",
        json={"approved": True},
    )
    client.patch(
        "/api/obsidian/note", params={"path": "Index.md"},
        json={"content": "Appended by JARVIS.", "mode": "append"},
    )
    text = (vault / "Index.md").read_text()
    assert "See [[Rust]]." in text and "Appended by JARVIS." in text


def test_delete_needs_its_own_switch_over_http(
    client: TestClient, vault: Path
) -> None:
    _connect(client, vault, allow_writes=True, allow_deletes=False)
    response = client.delete("/api/obsidian/note", params={"path": "Index.md"})
    assert response.status_code == 403
    assert (vault / "Index.md").exists()


def test_delete_asks_before_it_deletes(client: TestClient, vault: Path) -> None:
    _connect(client, vault, allow_writes=True, allow_deletes=True)

    first = client.delete("/api/obsidian/note", params={"path": "Index.md"}).json()
    assert first["status"] == "needs_confirmation"
    assert (vault / "Index.md").exists(), "asked and deleted anyway"

    client.post(
        f"/api/confirmations/{first['confirmation_id']}/decide",
        json={"approved": True},
    )
    second = client.delete("/api/obsidian/note", params={"path": "Index.md"}).json()
    assert second["deleted"] is True
    assert not (vault / "Index.md").exists()


def test_a_denied_confirmation_leaves_the_note_alone(
    client: TestClient, vault: Path
) -> None:
    _connect(client, vault, allow_writes=True, allow_deletes=True)
    first = client.delete("/api/obsidian/note", params={"path": "Index.md"}).json()
    client.post(
        f"/api/confirmations/{first['confirmation_id']}/decide",
        json={"approved": False},
    )
    second = client.delete("/api/obsidian/note", params={"path": "Index.md"}).json()
    assert second.get("status") == "needs_confirmation"
    assert (vault / "Index.md").exists()


# ── sync ─────────────────────────────────────────────────────────────────────


def test_sync_plan_then_sync(client: TestClient, vault: Path) -> None:
    _connect(client, vault)

    plan = client.get("/api/obsidian/sync/plan").json()
    assert len(plan["new"]) == 2 and plan["actionable"] == 2

    result = client.post("/api/obsidian/sync").json()
    assert result["indexed"] == 2

    again = client.get("/api/obsidian/sync/plan").json()
    assert again["actionable"] == 0 and again["unchanged_count"] == 2


def test_synced_notes_are_findable_through_knowledge_search(
    client: TestClient, vault: Path
) -> None:
    """The end of the pipeline: a vault note answers a question, with
    provenance that names the note."""
    _connect(client, vault)
    client.post("/api/obsidian/sync")

    body = client.get(
        "/api/knowledge/search", params={"q": "ownership borrowing"}
    ).json()
    assert body["results"]
    assert "Rust" in body["results"][0]["citation"]


def test_documents_list_shows_the_vault_provenance(
    client: TestClient, vault: Path
) -> None:
    _connect(client, vault)
    client.post("/api/obsidian/sync")
    documents = client.get("/api/knowledge/documents").json()["documents"]
    rust = next(d for d in documents if "Rust" in d["title"])
    assert rust["source_kind"] == "OBSIDIAN"
    assert rust["tainted"] is True


def test_sources_show_the_connected_vault(client: TestClient, vault: Path) -> None:
    _connect(client, vault)
    sources = {s["key"]: s for s in client.get("/api/knowledge/sources").json()["sources"]}
    key = next(k for k in sources if k.startswith("obsidian"))
    assert sources[key]["connected"] is True
    assert sources[key]["implemented"] is True


# ── audit (§21) ──────────────────────────────────────────────────────────────


def test_every_operation_is_audited(client: TestClient, vault: Path) -> None:
    _connect(client, vault, allow_writes=True)
    client.get("/api/obsidian/search", params={"q": "rust"})
    client.get("/api/obsidian/note", params={"path": "Index.md"})
    client.post("/api/obsidian/sync")

    entries = client.get("/api/obsidian/audit").json()["entries"]
    operations = {e["operation"] for e in entries}
    assert {"connect", "search", "read", "sync"} <= operations
    assert all(e["summary"] for e in entries)


def test_refusals_are_audited_too(client: TestClient, vault: Path) -> None:
    """A log that records only what succeeded answers the wrong question."""
    _connect(client, vault, allow_writes=False)
    client.post("/api/obsidian/notes", json={"title": "Nope", "content": "x"})

    entries = client.get("/api/obsidian/audit").json()["entries"]
    assert any(e["status"] == "DENIED" for e in entries)


def test_audit_has_no_write_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths.get("/api/obsidian/audit", {})) == {"get"}


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
        ("get", "/api/obsidian/status"),
        ("get", "/api/obsidian/discover"),
        ("get", "/api/obsidian/notes"),
        ("get", "/api/obsidian/folders"),
        ("get", "/api/obsidian/note"),
        ("get", "/api/obsidian/search"),
        ("get", "/api/obsidian/sync/plan"),
        ("get", "/api/obsidian/conflicts"),
        ("get", "/api/obsidian/audit"),
        ("post", "/api/obsidian/connect"),
        ("post", "/api/obsidian/test"),
        ("post", "/api/obsidian/disconnect"),
        ("post", "/api/obsidian/sync"),
        ("post", "/api/obsidian/notes"),
        ("patch", "/api/obsidian/note"),
        ("delete", "/api/obsidian/note"),
    ],
)
def test_every_obsidian_endpoint_requires_a_token(
    authed_client: TestClient, method: str, path: str
) -> None:
    response = getattr(authed_client, method)(
        path, **({"json": {}} if method in {"post", "patch"} else {})
    )
    assert response.status_code == 401, f"{method.upper()} {path} was not protected"


def test_status_never_leaks_a_full_path_or_a_credential(
    client: TestClient, vault: Path
) -> None:
    _connect(client, vault)
    raw = client.get("/api/obsidian/status").text
    assert str(vault) not in raw
    for marker in ("api_key", "password", "token", "secret"):
        assert marker not in raw.lower()
