"""The provider check earns its place by naming the cause (Phase D).

Written after a real failure: a freshly-configured key produced "The AI
provider had a problem" in the panel, which is `ProviderError`'s fallback
message — reachable from a bad model id, an empty credit balance, and anything
the translator does not recognise. Three different fixes behind one sentence.

Normalising provider errors is right for the chat panel and wrong for
debugging, so the fix is a separate diagnostic rather than a louder panel.
These tests hold it to the standard that makes it worth having: it must name
the cause, and it must never print the key.
"""

from __future__ import annotations

import pytest

from jarvis.config import Settings
from jarvis.diagnostics import (
    ModelProbe,
    ProviderReport,
    check_provider,
    render,
)


class _FakeStatusError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status_code = status


class _FakeModels:
    def __init__(self, ids: list[str], error: Exception | None = None) -> None:
        self._ids = ids
        self._error = error

    async def list(self, limit: int = 50):
        if self._error:
            raise self._error
        return type("Listing", (), {"data": [type("M", (), {"id": i}) for i in self._ids]})


class _FakeMessages:
    def __init__(self, failures: dict[str, Exception]) -> None:
        self._failures = failures

    async def create(self, *, model: str, **kwargs):
        if model in self._failures:
            raise self._failures[model]
        return object()


class _FakeClient:
    def __init__(self, ids, failures=None, list_error=None) -> None:
        self.models = _FakeModels(ids, list_error)
        self.messages = _FakeMessages(failures or {})


@pytest.fixture
def anthropic_client(monkeypatch):
    """Install a fake SDK client, so no network call and no key are needed."""

    def _install(**kwargs):
        import sys
        import types

        client = _FakeClient(**kwargs)
        module = types.ModuleType("anthropic")
        module.AsyncAnthropic = lambda api_key: client  # noqa: ARG005
        monkeypatch.setitem(sys.modules, "anthropic", module)
        return client

    return _install


@pytest.fixture
def key(monkeypatch):
    """A syntactically valid key resolved through the real secrets chain."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "x" * 20 + "WXYZ")


# ── it must never print the key ──────────────────────────────────────────────


async def test_the_key_never_appears_in_the_output(key, anthropic_client) -> None:
    """The whole point of a diagnostic is that its output gets pasted.

    Somewhere between the terminal and a chat window this text stops being
    private, and it is written by somebody who is already frustrated and not
    reading it closely. The tail is enough to tell "the one I pasted" from
    "some other key"; the rest is never needed to answer any question this
    tool asks.
    """
    anthropic_client(ids=["claude-sonnet-5"])
    output = render(await check_provider(Settings()))

    assert "sk-ant-api03-" not in output
    assert "x" * 20 not in output
    # …but enough to recognise it by.
    assert "WXYZ" in output
    assert "ending" in output


async def test_a_missing_key_is_distinguished_from_a_wrong_one(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    report = await check_provider(Settings())

    assert report.key_present is False
    assert "No API key was found" in report.verdict


@pytest.mark.parametrize(
    "value,expected",
    [('"sk-ant-api03-abcd"', "quotes"), ("hunter2", "sk-ant-")],
)
async def test_a_mispasted_key_is_named_as_such(monkeypatch, value, expected) -> None:
    """Both produce a 401 that reads as "wrong key" rather than "mispasted
    key", and the user goes and regenerates a key that was fine."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    report = await check_provider(Settings())

    assert report.key_present is True
    assert expected in report.verdict
    # The malformed value itself is not echoed back.
    assert "hunter2" not in render(report)


async def test_a_key_known_to_be_malformed_is_never_sent(monkeypatch) -> None:
    """The shape already answers the question, and a key with a quote welded
    to it is still most of a live credential. Pinned because the first version
    of this check set the message and then made the call anyway — which also
    meant the test suite was quietly talking to the API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", '"sk-ant-api03-abcd"')
    report = await check_provider(Settings())

    assert report.probes == []
    assert report.available_models == []
    assert report.models_error == ""


async def test_surrounding_whitespace_is_already_handled(monkeypatch) -> None:
    """A trailing space in .env is stripped by the secrets providers before
    anything sees it, so there is no warning to give — and giving one would
    describe a problem the user does not have."""
    from jarvis.diagnostics import _key

    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-api03-abcd  ")
    raw, _ = _key(Settings())

    assert raw == "sk-ant-api03-abcd"


# ── it must name the cause ───────────────────────────────────────────────────


async def test_an_empty_credit_balance_says_so(key, anthropic_client) -> None:
    """One of the two things that actually produced the fallback message.

    The remedy is billing, and nothing in "The AI provider had a problem"
    points there.
    """
    anthropic_client(
        ids=["claude-sonnet-5"],
        failures={
            m: _FakeStatusError(
                "Your credit balance is too low to access the Anthropic API", 400
            )
            for m in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")
        },
    )
    output = render(await check_provider(Settings()))

    assert "credit balance" in output
    assert "billing" in output


async def test_an_unreachable_model_is_shown_against_the_real_list(
    key, anthropic_client
) -> None:
    """The other one — and the reason the account's model list is fetched.

    "claude-opus-5 was rejected" is not actionable on its own. "…and here is
    what this account can call" is, and it beats anyone's memory of what the
    current model ids are.
    """
    anthropic_client(
        ids=["claude-sonnet-5", "claude-haiku-4-5"],
        failures={"claude-opus-5": _FakeStatusError("model: not_found", 404)},
    )
    report = await check_provider(Settings())
    output = render(report)

    assert "claude-sonnet-5" in output and "claude-haiku-4-5" in output
    assert "not available to this account" in output
    # A working model is named, so the fix does not need a second lookup.
    assert "claude-sonnet-5" in report.verdict


async def test_a_healthy_provider_says_nothing_alarming(key, anthropic_client) -> None:
    anthropic_client(ids=["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"])
    report = await check_provider(Settings())

    assert all(probe.ok for probe in report.probes)
    assert "healthy" in report.verdict
    assert "FAIL" not in render(report)


async def test_every_configured_role_is_probed(key, anthropic_client) -> None:
    """Three roles, three calls. Probing only one would let a broken reasoning
    model hide behind a working conversation model — and reasoning is the one
    a turn reaches first."""
    anthropic_client(ids=["claude-sonnet-5"])
    report = await check_provider(Settings())

    assert {p.role for p in report.probes} == {"reasoning", "conversation", "fast"}


async def test_a_rejected_key_is_reported_before_any_model_is_blamed(
    key, anthropic_client
) -> None:
    """Listing models fails first on a bad key, and the report stops there
    rather than printing three model failures that all share one cause."""
    anthropic_client(
        ids=[], list_error=_FakeStatusError("invalid x-api-key", 401)
    )
    report = await check_provider(Settings())

    assert report.probes == []
    assert "key was rejected" in report.verdict


# ── it is a diagnostic, not a code path ──────────────────────────────────────


def test_nothing_in_the_running_system_imports_it() -> None:
    """A probe that fires during a turn would spend tokens and add latency for
    a question nobody asked."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "jarvis"
    importers = []
    for path in root.rglob("*.py"):
        if path.name == "diagnostics.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "diagnostics" in node.module:
                    importers.append(path.name)
            elif isinstance(node, ast.Import):
                if any("diagnostics" in a.name for a in node.names):
                    importers.append(path.name)

    assert importers == [], f"diagnostics is imported by {importers}"


def test_the_probe_costs_one_token() -> None:
    """It asks whether the call is *accepted*. Anthropic validates the model,
    the credentials and the balance before generating anything, so a longer
    probe would buy the same answer and bill for it."""
    from jarvis.diagnostics import PROBE_MAX_TOKENS

    assert PROBE_MAX_TOKENS == 1


def test_a_report_with_no_probes_still_renders() -> None:
    """The no-key path reaches `render` before anything is populated, and a
    diagnostic that raises while diagnosing is a poor showing."""
    assert "NOT FOUND" in render(ProviderReport())
    assert ModelProbe(role="fast", model="m", ok=True).describe().strip().startswith("OK")
