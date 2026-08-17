"""Choosing the model from the console (Phase D).

Model choice lived in ``.env`` and was read once at startup, so "use a stronger
model for planning" meant a text editor and a restart. This is the control that
replaces that, and these tests exist to hold the line the control sits closest
to: the frontend may never widen what JARVIS is permitted to do.

The claim being tested is that a model id is not an authority. It selects who
does the thinking; the thinking still leaves through ``ToolExecutor`` and the
permission engine. Three properties make that true rather than merely intended,
and each has a test below:

* only ids a configured provider declares can be stored;
* a preference cannot change which *provider* handles a call, and therefore
  cannot defeat ``must_stay_local``;
* nothing about capabilities, grants or confirmations moves when it changes.
"""

from __future__ import annotations

import pytest

from jarvis.config import Settings
from jarvis.providers import preferences
from jarvis.providers.registry import ProviderRegistry
from jarvis.providers.router import ModelRouter, RoutingConstraints, TaskClass


# ── the frontend cannot widen authority ──────────────────────────────────────


def test_the_request_schema_cannot_express_anything_but_a_model() -> None:
    """The rule, enforced by the type rather than by a check to remember.

    No provider, no base url, no api key, no capability. A caller who wanted to
    point JARVIS at an arbitrary endpoint has no field to say it in, which is a
    stronger guarantee than validating one.
    """
    from jarvis.api.routes import SetModelRequest

    assert set(SetModelRequest.model_fields) == {"role", "model"}


def test_only_three_roles_are_selectable() -> None:
    """STRUCTURED follows conversation rather than getting a fourth control.

    Not laziness: it is a distinction most people do not have, and four
    decisions where three will do is how a settings panel stops being read.
    """
    assert set(preferences.SELECTABLE_ROLES) == {
        TaskClass.REASONING, TaskClass.CONVERSATION, TaskClass.FAST
    }


async def test_an_unknown_model_is_refused_not_stored(client, core, stub) -> None:
    """An id stored here would be sent to the vendor on the next turn and fail
    *there* — a long way from its cause, and exactly the confusing failure the
    provider diagnostic was written to untangle."""
    body = {"role": "REASONING", "model": "gpt-5-turbo-ultra"}
    response = client.patch("/api/system/models", json=body)

    assert response.status_code == 422
    assert "not offered by any configured provider" in response.json()["detail"]
    # And the router is untouched, not merely the response unhappy.
    assert core.router._model_for(TaskClass.REASONING) != "gpt-5-turbo-ultra"


async def test_a_preference_cannot_change_the_provider(core, stub) -> None:
    """The property that makes this safe to expose at all.

    `_resolve_model` runs *after* provider selection and falls back to the
    chosen provider's default when the candidate is not its own. So naming
    another vendor's model cannot route the call to that vendor — which is also
    why a preference cannot defeat `must_stay_local`, a filter over providers
    rather than over models.
    """
    router = core.router
    router.set_overrides({TaskClass.REASONING: "claude-opus-5"})

    decision = router.select(TaskClass.REASONING)

    assert decision.provider.key == stub.key
    # The stub does not declare that model, so its own default is used rather
    # than a name it would choke on.
    assert decision.model in stub.models


async def test_a_preference_cannot_defeat_must_stay_local(core, stub) -> None:
    """Stated separately because it is the one constraint that *refuses*
    rather than degrading, and it is the one somebody would reach for the
    console to get around."""
    router = core.router
    router.set_overrides({TaskClass.CONVERSATION: "claude-opus-5"})

    if stub.runs_locally:
        pytest.skip("the stub is local, so there is nothing to exclude")

    from jarvis.errors import NoEligibleProviderError

    with pytest.raises(NoEligibleProviderError):
        router.select(
            TaskClass.CONVERSATION,
            constraints=RoutingConstraints(must_stay_local=True),
        )


async def test_changing_the_model_moves_no_permission(client, session) -> None:
    """The claim in one assertion: the cage is the same size afterwards."""
    before = client.get("/api/permissions").json()

    models = client.get("/api/system/models").json()
    if models["available"]:
        client.patch("/api/system/models", json={
            "role": "REASONING", "model": models["available"][0]["id"],
        })

    assert client.get("/api/permissions").json() == before


# ── it does what it says ─────────────────────────────────────────────────────


async def test_a_choice_survives_a_restart_and_is_actually_in_force(
    core, stub
) -> None:
    """Two halves of one promise, and the second is the one that would rot.

    A preference stored but never pushed onto the router would show correctly
    in the panel while every turn quietly used the .env default — the same
    class of defect as the Windows backend that was wired in and unreachable.
    """
    from jarvis.core import JarvisCore

    model = next(iter(stub.models))

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        preferences.set_model(user, TaskClass.FAST, model,
                              registry=core.providers, router=core.router)
        await session.commit()

    # Restart: a fresh router with nothing in it, then startup's reload.
    core.router.set_overrides({})
    assert core.router._model_for(TaskClass.FAST) != model, "not yet reloaded"

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        preferences.apply_to(core.router, preferences.stored(user))

    assert core.router._model_for(TaskClass.FAST) == model


async def test_clearing_a_role_returns_it_to_the_configured_default(
    core, stub
) -> None:
    """`.env` stays the floor, so a bad choice is one click from recovery
    rather than a file edit."""
    from jarvis.core import JarvisCore

    model = next(iter(stub.models))
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        preferences.set_model(user, TaskClass.REASONING, model,
                              registry=core.providers, router=core.router)
        cleared = preferences.set_model(user, TaskClass.REASONING, None,
                                        registry=core.providers,
                                        router=core.router)

    assert cleared.source == "config"
    assert cleared.model == core.settings.model_reasoning
    assert core.router._model_for(TaskClass.REASONING) == core.settings.model_reasoning


def test_overrides_are_replaced_rather_than_merged() -> None:
    """A merge would leave a preference the user believes they removed still in
    force, which is the worst kind of setting: invisible and load-bearing."""
    router = ModelRouter(ProviderRegistry(), Settings(environment="test"))
    router.set_overrides({TaskClass.FAST: "a", TaskClass.REASONING: "b"})
    router.set_overrides({TaskClass.FAST: "a"})

    assert TaskClass.REASONING not in router._overrides


def test_structured_work_follows_the_conversation_choice() -> None:
    """Otherwise picking a conversation model would silently leave JSON work on
    the .env default — a split nobody asked for and nobody would see."""
    router = ModelRouter(ProviderRegistry(), Settings(environment="test"))
    router.set_overrides({TaskClass.CONVERSATION: "chosen"})

    assert router._model_for(TaskClass.STRUCTURED) == "chosen"


async def test_only_callable_models_are_offered(client, stub) -> None:
    """The dropdown is built from the registry, so it cannot contain an option
    that fails when selected. A short list beats a broken one."""
    body = client.get("/api/system/models").json()

    assert body["available"], "no models offered at all"
    for model in body["available"]:
        assert model["id"] in stub.models
        assert model["provider"] == stub.key


async def test_the_panel_says_where_each_choice_came_from(client, stub) -> None:
    """"Why is it using that?" should not need reading two places."""
    body = client.get("/api/system/models").json()
    assert {r["source"] for r in body["roles"]} == {"config"}

    client.patch("/api/system/models", json={
        "role": "FAST", "model": next(iter(stub.models)),
    })
    after = {r["role"]: r for r in client.get("/api/system/models").json()["roles"]}

    assert after["FAST"]["source"] == "preference"
    assert after["REASONING"]["source"] == "config"


async def test_a_change_is_recorded(client, core, stub) -> None:
    """Not dangerous, and still worth being able to find in a log when a bill
    looks surprising."""
    client.patch("/api/system/models", json={
        "role": "REASONING", "model": next(iter(stub.models)),
    })

    activity = client.get("/api/activity?limit=20").json()
    summaries = " ".join(a.get("summary", "") for a in activity["activity"])
    assert "Model for REASONING set to" in summaries


@pytest.fixture
def locked_client(core, monkeypatch):
    """Auth switched on, no token supplied. Defined here rather than imported
    from another test file, for the reason its twin in test_console.py gives:
    a fixture shared by import silently changes when the other file's needs
    change."""
    from fastapi.testclient import TestClient

    from jarvis.api.app import create_app
    from jarvis.config import reset_config_caches

    monkeypatch.setenv("JARVIS_API_TOKEN", "test-token-abcdefghijklmnop")
    reset_config_caches()
    settings = Settings(environment="test",
                        database_url="sqlite+aiosqlite:///:memory:",
                        require_auth=True, log_level="CRITICAL")
    core.settings = settings
    with TestClient(create_app(settings, core=core)) as c:
        yield c


def test_the_endpoints_require_a_token(locked_client) -> None:
    """Both of them. A readable model list is minor; a writable one lets
    anything that can reach the port move spend onto the priciest model."""
    assert locked_client.get("/api/system/models").status_code == 401
    assert locked_client.patch(
        "/api/system/models", json={"role": "FAST", "model": "x"}
    ).status_code == 401


def test_the_console_sends_no_provider_or_credential(client) -> None:
    """The panel's request body, pinned by source.

    A control that grew a provider field later would be the actual violation of
    the frontend rule, and it would look like an ordinary feature in review.
    """
    import re

    source = client.get("/assets/app.js").text
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

    picker = re.search(r"function modelPicker\(.*?\n  \}", code, flags=re.S)
    assert picker, "modelPicker moved"
    for forbidden in ("api_key", "base_url", "provider:", "capability",
                      "ANTHROPIC"):
        assert forbidden not in picker.group(0), forbidden


def test_a_smuggled_provider_field_is_refused_not_ignored(client) -> None:
    """Pydantic's default drops unknown fields and returns 200, which reads to
    the caller as though the whole body was honoured.

    Found by exercising the endpoint by hand rather than by a test: a PATCH
    carrying `provider` and `base_url` succeeded, having quietly done something
    narrower than asked. Nothing unsafe happened — but "we ignored the part
    that mattered to you" is not something an API should say with a 200.
    """
    response = client.patch("/api/system/models", json={
        "role": "REASONING",
        "model": "claude-opus-5",
        "provider": "somewhere-else",
        "base_url": "http://attacker.example",
    })

    assert response.status_code == 422


# ── local models (Phase D) ───────────────────────────────────────────────────


def test_a_local_runtime_offers_every_model_it_was_configured_with() -> None:
    """Found by wiring Ollama up rather than by reading the code.

    Only the *default* model was recorded, so an OpenAI-compatible provider
    reported an empty `models` dict. Harmless for routing — the router falls
    back to `default_model` — and not harmless for anything that asks a
    provider what it offers. The console's picker does, so a machine running
    Ollama got an empty dropdown and no way to reach the second model it had
    pulled. That is precisely the setup somebody chooses when they do not want
    to pay per token, which makes it the worst place for the list to be blank.
    """
    from jarvis.providers.registry import build_registry

    settings = Settings(
        environment="test",
        openai_base_url="http://127.0.0.1:11434/v1",
        openai_compat_models=["llama3.1", "qwen2.5"],
    )
    offered = preferences.selectable_models(build_registry(settings))

    assert {m["id"] for m in offered} == {"llama3.1", "qwen2.5"}
    assert all(m["runs_locally"] for m in offered)
    assert all(m["input_price_per_mtok"] == 0.0 for m in offered), \
        "a local model costs nothing per token and should sort first"


def test_a_local_model_declares_no_context_window_rather_than_a_guess() -> None:
    """`min_context_tokens` treats an undeclared window as "do not exclude".

    Inventing a large one would let the constraint select a provider that then
    truncates silently — a lie the model never mentions, and the hardest kind
    to notice.
    """
    from jarvis.providers.registry import _declared_models

    info = _declared_models(["llama3.1"])["llama3.1"]

    assert info.context_window == 0
    assert info.max_output_tokens == 0
