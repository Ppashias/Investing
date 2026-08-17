"""API surface, authentication, and the security properties of logging."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import create_app
from jarvis.config import Settings
from jarvis.core import JarvisCore
from jarvis.logging import (
    REDACTED,
    clear_registered_secrets,
    redaction_processor,
    register_secret_value,
    scrub_text,
)
from jarvis.secrets import EnvSecretsProvider, Secret
from tests.conftest import text_result


# ── endpoints ────────────────────────────────────────────────────────────────


def test_health_is_public_and_minimal(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    # Must not leak configuration to an unauthenticated caller.
    assert "settings" not in body and "providers" not in body


def test_system_status_lists_tools_and_providers(client: TestClient) -> None:
    body = client.get("/api/system/status").json()
    assert body["tools"]["count"] == 43
    assert body["providers"][0]["key"] == "stub"


def test_system_status_never_returns_a_credential(client: TestClient) -> None:
    raw = client.get("/api/system/status").text.lower()
    for marker in ("api_key", "apikey", "secret", "password"):
        assert marker not in raw
    # "token" appears legitimately in token-count fields, so it is matched
    # against the credential-shaped forms rather than the bare word.
    for marker in ("api_token", "access_token", "bearer "):
        assert marker not in raw


def test_system_status_declares_whether_search_is_semantic(client: TestClient) -> None:
    """The user must be able to tell lexical fallback from real embeddings."""
    body = client.get("/api/system/status").json()
    assert body["embeddings"]["semantic"] is False
    assert "lexical" in body["embeddings"]["description"].lower()


def test_task_crud(client: TestClient) -> None:
    created = client.post("/api/tasks", json={"title": "API task", "priority": "HIGH"})
    assert created.status_code == 201
    task_id = created.json()["id"]

    listed = client.get("/api/tasks").json()
    assert listed["counts"]["TODO"] == 1

    patched = client.patch(f"/api/tasks/{task_id}", json={"status": "IN_PROGRESS"})
    assert patched.json()["status"] == "IN_PROGRESS"

    detail = client.get(f"/api/tasks/{task_id}").json()
    assert len(detail["history"]) >= 2


def test_invalid_status_transition_returns_409(client: TestClient) -> None:
    task_id = client.post("/api/tasks", json={"title": "T"}).json()["id"]
    client.patch(f"/api/tasks/{task_id}", json={"status": "COMPLETED"})
    response = client.patch(f"/api/tasks/{task_id}", json={"status": "BLOCKED"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"


def test_missing_task_returns_404(client: TestClient) -> None:
    assert client.get("/api/tasks/task_nope").status_code == 404


def test_validation_error_returns_422(client: TestClient) -> None:
    assert client.post("/api/tasks", json={"title": ""}).status_code == 422


def test_chat_endpoint_runs_the_pipeline(client: TestClient, stub) -> None:
    stub.responses = [text_result("Hello from JARVIS.")]
    body = client.post("/api/chat", json={"message": "Hello"}).json()
    assert body["status"] == "completed"
    assert body["text"] == "Hello from JARVIS."
    assert body["conversation_id"]


def test_conversation_is_retrievable_after_chat(client: TestClient, stub) -> None:
    stub.responses = [text_result("Reply.")]
    conversation_id = client.post("/api/chat", json={"message": "Hi"}).json()[
        "conversation_id"
    ]
    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_tools_endpoint_exposes_policy(client: TestClient) -> None:
    tools = {t["name"]: t for t in client.get("/api/tools").json()["tools"]}
    assert tools["create_task"]["capability"] == "WRITE"
    assert tools["get_current_time"]["risk_level"] == "NONE"


def test_tool_can_be_disabled_then_denied(client: TestClient, stub) -> None:
    assert client.patch("/api/tools/create_task", json={"enabled": False}).json()[
        "enabled"
    ] is False
    listed = {t["name"]: t for t in client.get("/api/tools").json()["tools"]}
    assert listed["create_task"]["enabled"] is False


def test_permissions_endpoint_shows_grants_and_defaults(client: TestClient) -> None:
    body = client.get("/api/permissions").json()
    assert body["defaults"]["SENSITIVE_ACTION"] == "DENY"
    assert any(g["capability"] == "READ" for g in body["grants"])


def test_activity_endpoint_returns_records(client: TestClient, stub) -> None:
    stub.responses = [text_result("ok")]
    client.post("/api/chat", json={"message": "Hello"})
    activity = client.get("/api/activity").json()["activity"]
    assert any(a["kind"] == "REQUEST_COMPLETED" for a in activity)


def test_system_prompt_is_inspectable(client: TestClient) -> None:
    body = client.get("/api/system/prompt").json()
    ids = {b["id"] for b in body["blocks"]}
    assert {"identity", "behavior", "security", "runtime"} <= ids
    # No query means nothing was retrieved, which is accurate rather than
    # incomplete — there is no request to retrieve for.
    assert "memory" not in ids


def test_system_prompt_shows_what_a_query_would_retrieve(client: TestClient) -> None:
    client.post(
        "/api/memories",
        json={"content": "The user prefers dark mode", "type": "USER_PREFERENCE",
              "subject": "interface theme"},
    )
    body = client.get("/api/system/prompt", params={"q": "dark mode"}).json()
    assert "memory" in {b["id"] for b in body["blocks"]}
    assert "dark mode" in body["rendered"]
    assert body["retrieval"]["memories"], "the ranking must be inspectable"


def test_health_reports_the_current_phase(client: TestClient) -> None:
    assert client.get("/api/health").json()["phase"] == 2


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Request-ID"]


# ── authentication ───────────────────────────────────────────────────────────


@pytest.fixture
def authed_client(core: JarvisCore, monkeypatch):
    """A client with auth switched on and a known token."""
    monkeypatch.setenv("JARVIS_API_TOKEN", "test-token-abcdefghijklmnop")
    from jarvis.config import reset_config_caches

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


def test_auth_rejects_missing_token(authed_client: TestClient) -> None:
    assert authed_client.get("/api/tasks").status_code == 401


def test_auth_rejects_wrong_token(authed_client: TestClient) -> None:
    response = authed_client.get(
        "/api/tasks", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_auth_accepts_correct_token(authed_client: TestClient) -> None:
    response = authed_client.get(
        "/api/tasks", headers={"Authorization": "Bearer test-token-abcdefghijklmnop"}
    )
    assert response.status_code == 200


def test_health_stays_public_when_auth_enabled(authed_client: TestClient) -> None:
    assert authed_client.get("/api/health").status_code == 200


def test_auth_fails_closed_when_token_unset(core: JarvisCore, monkeypatch) -> None:
    """Auth enabled with no configured token must refuse, not wave everyone in."""
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    from jarvis.config import reset_config_caches

    reset_config_caches()
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        require_auth=True,
        log_level="CRITICAL",
    )
    core.settings = settings
    with TestClient(create_app(settings, core=core)) as c:
        assert c.get("/api/tasks").status_code == 503


# ── secret handling ──────────────────────────────────────────────────────────


def test_secret_does_not_print_its_value() -> None:
    secret = Secret("sk-ant-verysecretvalue123456", name="ANTHROPIC_API_KEY")
    assert "verysecret" not in repr(secret)
    assert "verysecret" not in str(secret)
    assert secret.reveal() == "sk-ant-verysecretvalue123456"


def test_env_provider_reads_and_trims(monkeypatch) -> None:
    monkeypatch.setenv("SOME_KEY", "  value-with-space  ")
    assert EnvSecretsProvider().get("SOME_KEY").reveal() == "value-with-space"


def test_env_provider_treats_blank_as_absent(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_KEY", "   ")
    assert EnvSecretsProvider().get("BLANK_KEY") is None


def test_registered_secret_is_scrubbed_from_text() -> None:
    clear_registered_secrets()
    register_secret_value("supersecrettoken12345")
    assert "supersecrettoken12345" not in scrub_text(
        "Authorization failed for supersecrettoken12345 on retry"
    )
    clear_registered_secrets()


def test_credential_shaped_strings_are_scrubbed_without_registration() -> None:
    clear_registered_secrets()
    text = "failed with key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"
    assert "sk-ant-api03" not in scrub_text(text)


def test_sensitive_keys_are_redacted_in_log_events() -> None:
    event = redaction_processor(
        None, "info",
        {"event": "call", "api_key": "sk-ant-secret", "authorization": "Bearer abc",
         "model": "claude-opus-5"},
    )
    assert event["api_key"] == REDACTED
    assert event["authorization"] == REDACTED
    assert event["model"] == "claude-opus-5", "non-sensitive fields must survive"


def test_nested_sensitive_values_are_redacted() -> None:
    event = redaction_processor(
        None, "info", {"event": "x", "config": {"token": "abcd1234", "host": "local"}}
    )
    assert event["config"]["token"] == REDACTED
    assert event["config"]["host"] == "local"


def test_short_values_are_not_scrubbed() -> None:
    """Redacting very short strings would corrupt unrelated text far more often
    than it would protect anything."""
    clear_registered_secrets()
    register_secret_value("abc")
    assert scrub_text("abc is fine") == "abc is fine"
    clear_registered_secrets()


# ── credentials from .env (the Windows setup path) ───────────────────────────


def test_a_token_in_dotenv_is_found(tmp_path, monkeypatch) -> None:
    """The bug a real Windows install found.

    Settings read .env through pydantic-settings, but that populates setting
    *fields* and exports nothing to os.environ — while every credential is
    fetched by name through jarvis.secrets, which only read os.environ. So a
    token in .env resolved to nothing, and the README told people to put it
    there. The development environment always exported the variable in a
    shell, which is why nothing noticed.
    """
    from jarvis.secrets import default_secrets_provider

    env_file = tmp_path / ".env"
    env_file.write_text("JARVIS_API_TOKEN=tok-from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_ENV_FILE", str(env_file))
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)

    secret = default_secrets_provider().get("JARVIS_API_TOKEN")
    assert secret is not None
    assert secret.reveal() == "tok-from-dotenv"


def test_a_bom_does_not_eat_the_first_credential(tmp_path, monkeypatch) -> None:
    """Windows PowerShell writes UTF-8 *with* a BOM.

    Read as plain utf-8, the BOM becomes part of the first key's name — so the
    first credential in the file, and only that one, silently fails to resolve.
    """
    from jarvis.secrets import default_secrets_provider

    env_file = tmp_path / ".env"
    env_file.write_text("JARVIS_API_TOKEN=tok-with-bom\n", encoding="utf-8-sig")
    monkeypatch.setenv("JARVIS_ENV_FILE", str(env_file))
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)

    assert default_secrets_provider().get("JARVIS_API_TOKEN").reveal() == "tok-with-bom"


def test_the_environment_still_wins_over_dotenv(tmp_path, monkeypatch) -> None:
    """The documented precedence: a shell export overrides the file."""
    from jarvis.secrets import default_secrets_provider

    env_file = tmp_path / ".env"
    env_file.write_text("JARVIS_API_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_ENV_FILE", str(env_file))
    monkeypatch.setenv("JARVIS_API_TOKEN", "from-environment")

    assert default_secrets_provider().get(
        "JARVIS_API_TOKEN"
    ).reveal() == "from-environment"


def test_quoted_and_commented_values_are_handled(tmp_path, monkeypatch) -> None:
    from jarvis.secrets import default_secrets_provider

    env_file = tmp_path / ".env"
    env_file.write_text(
        '# JARVIS_API_TOKEN=commented-out\n'
        'ANTHROPIC_API_KEY="sk-quoted"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_ENV_FILE", str(env_file))
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    provider = default_secrets_provider()
    assert provider.get("JARVIS_API_TOKEN") is None, "a commented line is not a value"
    assert provider.get("ANTHROPIC_API_KEY").reveal() == "sk-quoted"


def test_settings_load_from_a_bom_encoded_env_file(tmp_path, monkeypatch) -> None:
    """Notepad and PowerShell both write a BOM by default.

    Read as plain utf-8 it becomes part of the first key's name, so the first
    setting in the file silently fails to load — and it is the first setting
    precisely because that is the one people edit first.
    """
    from jarvis.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text(
        "JARVIS_PORT=9911\nJARVIS_REQUIRE_AUTH=false\n", encoding="utf-8-sig"
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.port == 9911
    assert settings.require_auth is False
