"""Choosing the model from the console, without choosing anything else.

Model choice lived in ``.env`` and was read once at startup, which made "use a
stronger model for planning" a text-editor-and-restart operation. That is the
wrong shape for the one setting a user genuinely wants to fiddle with: it is
reversible, it costs nothing to try, and the right answer depends on what they
are doing this afternoon.

## Why this is not a hole in the frontend rule

The Command Center brief says the frontend must never widen an agent's
authority ceiling or edit the authorization rules. This does neither, and the
distinction is worth being precise about rather than asserting.

A model id selects *who does the thinking*. It does not touch capabilities,
grants, scopes, confirmation thresholds, or the irreversibility floor. Every
tool call a stronger model makes goes through ``ToolExecutor`` and the
permission engine exactly as before, and is refused in exactly the same cases.
Swapping Sonnet for Opus buys better reasoning inside the same cage.

Three properties keep it that way, and each has a test:

* **Allowlist, never free text.** Only ids a configured provider declares can
  be stored. An arbitrary string would be forwarded to the vendor and produce
  the confusing 404 that motivated this module's sibling, ``diagnostics``.
* **The provider is chosen first, and a preference cannot change it.**
  ``ModelRouter._resolve_model`` runs after provider selection and falls back
  to that provider's default when the candidate is not its own, so a
  preference cannot move a call to a different vendor — and cannot defeat
  ``must_stay_local``, which filters providers, not models.
* **Recorded.** A model change is an ordinary audited event. It is not
  dangerous, and it is the sort of thing somebody should be able to notice in
  a log when a bill looks odd.

## Where it is stored

``User.settings``, a JSON column that already exists — no migration, and the
preference travels with the subject that authorised it. ``.env`` remains the
default and the floor: clearing a role returns it to the configured value
rather than to nothing, so a broken preference is one click from recovery
rather than a file edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.db.models import User
from jarvis.logging import get_logger
from jarvis.providers.registry import ProviderRegistry
from jarvis.providers.router import ModelRouter, TaskClass

log = get_logger(__name__)

#: The roles a user may set. ``STRUCTURED`` is deliberately absent: it follows
#: conversation, and exposing a fourth control for a distinction most people do
#: not have would be four decisions where three will do.
SELECTABLE_ROLES: tuple[TaskClass, ...] = (
    TaskClass.REASONING,
    TaskClass.CONVERSATION,
    TaskClass.FAST,
)

#: Key within ``User.settings``.
SETTINGS_KEY = "models"


class UnknownModel(ValueError):
    """A model id no configured provider declares."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One role, what it resolves to, and whether a person chose it."""

    role: str
    model: str
    #: ``"preference"`` when the user set it, ``"config"`` when it came from
    #: ``.env``. Shown in the UI so "why is it using that?" has an answer
    #: without reading two places.
    source: str

    def describe(self) -> dict[str, Any]:
        return {"role": self.role, "model": self.model, "source": self.source}


def selectable_models(registry: ProviderRegistry) -> list[dict[str, Any]]:
    """Every model a configured provider can actually call.

    Built from the registry rather than from a hardcoded list, so a new
    provider's models appear without editing this module — and so nothing
    unreachable is ever offered. A dropdown containing an option that fails is
    worse than a shorter dropdown.
    """
    found: list[dict[str, Any]] = []
    for provider in registry.all():
        if not provider.is_configured():
            continue
        for model_id, info in provider.models.items():
            found.append({
                "id": model_id,
                "provider": provider.key,
                "context_window": getattr(info, "context_window", None),
                "input_price_per_mtok": getattr(info, "input_price_per_mtok", None),
                "runs_locally": provider.runs_locally,
            })
    # Cheapest first, undeclared prices last: the list is read top-down and the
    # expensive option should be a deliberate scroll rather than the default
    # thing under the cursor.
    found.sort(key=lambda m: (m["input_price_per_mtok"] is None,
                              m["input_price_per_mtok"] or 0.0, m["id"]))
    return found


def stored(user: User) -> dict[str, str]:
    raw = (user.settings or {}).get(SETTINGS_KEY) or {}
    return {k: v for k, v in raw.items() if isinstance(v, str)}


def current(user: User, router: ModelRouter) -> list[ModelChoice]:
    """What each role resolves to right now, and why."""
    overrides = stored(user)
    choices = []
    for role in SELECTABLE_ROLES:
        chosen = overrides.get(role.value)
        choices.append(
            ModelChoice(
                role=role.value,
                model=chosen or router.configured_model(role),
                source="preference" if chosen else "config",
            )
        )
    return choices


def set_model(
    user: User,
    role: TaskClass,
    model: str | None,
    *,
    registry: ProviderRegistry,
    router: ModelRouter,
) -> ModelChoice:
    """Point a role at a model, or clear it back to the configured default.

    Validated against the registry before anything is written. An unknown id
    stored here would be forwarded to the vendor on the next turn and fail
    there, which puts the error a long way from its cause.
    """
    if role not in SELECTABLE_ROLES:
        raise UnknownModel(f"{role.value} is not a selectable role")

    preferences = dict(stored(user))

    if model is None:
        preferences.pop(role.value, None)
    else:
        allowed = {m["id"] for m in selectable_models(registry)}
        if model not in allowed:
            raise UnknownModel(
                f"'{model}' is not offered by any configured provider. "
                f"Available: {', '.join(sorted(allowed)) or 'none'}."
            )
        preferences[role.value] = model

    # Reassigned rather than mutated: SQLAlchemy does not track in-place
    # changes to a JSON column, so mutating settings["models"] would look
    # correct in memory and never reach the database.
    settings = dict(user.settings or {})
    settings[SETTINGS_KEY] = preferences
    user.settings = settings

    apply_to(router, preferences)
    log.info("model_preference_set", role=role.value, model=model or "(default)")
    return next(c for c in current(user, router) if c.role == role.value)


def apply_to(router: ModelRouter, preferences: dict[str, str]) -> None:
    """Push stored preferences onto the live router.

    The router is a process-wide singleton constructed at startup, so a
    preference written to the database changes nothing until this runs. Called
    on every write and once at startup — a setting that only takes effect after
    a restart is the problem this module set out to remove.
    """
    router.set_overrides({
        role: preferences[role.value]
        for role in SELECTABLE_ROLES
        if role.value in preferences
    })
