"""Answering "why did the provider fail?" without reading the source.

JARVIS deliberately normalises provider failures into its own taxonomy before
they reach the user: :class:`~jarvis.errors.ProviderError` and friends carry a
short ``default_user_message`` and the vendor's text stays in the log. That is
the right default — a chat panel is not a stack trace, and an unfiltered
provider message can carry account identifiers, request ids, and occasionally
the prompt back.

The cost showed up the first time somebody configured a key: the panel said
"The AI provider had a problem", which is the *fallback* message, and telling
apart the handful of things that produce it meant reading `_translate_error`.
The two that actually happen are a model the account cannot reach and an empty
credit balance, and they need opposite fixes.

So this module asks the API directly and prints what it says. It is a
diagnostic, not a code path: nothing in the running system imports it, and it
takes no part in a turn.

## What it will not print

The key. It reports the length and the last four characters, which is enough to
tell "not loaded" from "loaded but wrong" and from "the one I pasted", and not
enough to use. Diagnostic output gets pasted into chat windows and issue
trackers by people who are already having a bad afternoon.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from jarvis.config import Settings
from jarvis.secrets import default_secrets_provider

#: A single token, because the question is whether the call is *accepted*.
#: Anthropic validates the model, the credentials and the balance before it
#: generates anything, so one token buys the same answer as a thousand.
PROBE_MAX_TOKENS = 1

PROBE_PROMPT = "Reply with the single word: ok"


@dataclass(slots=True)
class ModelProbe:
    """One model, and whether this account can call it."""

    role: str
    model: str
    ok: bool = False
    error: str = ""

    def describe(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        line = f"    {mark} {self.role:<13} {self.model}"
        return line if self.ok else f"{line}\n         {self.error}"


@dataclass(slots=True)
class ProviderReport:
    key_present: bool = False
    key_length: int = 0
    key_tail: str = ""
    key_source: str = ""
    key_looks_malformed: str = ""
    available_models: list[str] = field(default_factory=list)
    models_error: str = ""
    probes: list[ModelProbe] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.key_present:
            return (
                "No API key was found. Add ANTHROPIC_API_KEY to .env and "
                "restart JARVIS."
            )
        if self.key_looks_malformed:
            return self.key_looks_malformed
        if self.models_error:
            return self.models_error
        broken = [p for p in self.probes if not p.ok]
        if not broken:
            return "Every configured model answered. The provider is healthy."
        if all(not p.ok for p in self.probes):
            return (
                "No configured model could be called. If the errors above "
                "mention credit or billing, add funds at "
                "console.anthropic.com/settings/billing. If they mention the "
                "model, pick one from the available list above and set it in "
                ".env."
            )
        working = next(p for p in self.probes if p.ok)
        return (
            f"{len(broken)} of {len(self.probes)} models failed. Set the "
            f"failing roles to a model that works, e.g. {working.model}."
        )


def _key(settings: Settings) -> tuple[str | None, str]:
    """The key JARVIS itself would use, and where it came from.

    Resolved through the same chain the application uses rather than by reading
    .env here — a diagnostic that looks somewhere else can report a key the
    running system never sees, which is worse than no diagnostic.
    """
    provider = default_secrets_provider()
    for backend in provider.providers:
        found = backend.get(settings.anthropic_api_key_name)
        if found is not None:
            # `reveal()` rather than str(): Secret redacts both __str__ and
            # __repr__ deliberately, and the explicit call is what makes secret
            # use greppable in review. This is one of the few places entitled
            # to it — and note that nothing below puts the result in output.
            return found.reveal(), backend.name
    return None, ""


async def check_provider(settings: Settings | None = None) -> ProviderReport:
    settings = settings or Settings()
    report = ProviderReport()

    raw, source = _key(settings)
    if not raw:
        return report

    report.key_present = True
    report.key_source = source
    report.key_length = len(raw)
    report.key_tail = raw[-4:]

    # Cheap shape checks for the mistakes a text editor makes, both of which
    # produce a 401 that reads like a wrong key rather than a mispasted one —
    # and sends the user off to regenerate a key that was fine.
    #
    # Surrounding whitespace is not among them: the secrets providers strip it
    # before anything sees it, so a trailing space in .env is already handled
    # and warning about it would describe a problem the user does not have.
    if raw.startswith(('"', "'")) or raw.endswith(('"', "'")):
        report.key_looks_malformed = (
            "The key is wrapped in quotes. .env values are literal — delete the "
            "quotes and restart."
        )
    elif not raw.startswith("sk-ant-"):
        report.key_looks_malformed = (
            "The key does not start with 'sk-ant-'. That is the shape Anthropic "
            "issues; check you pasted an API key rather than something else."
        )

    if report.key_looks_malformed:
        # Stop here rather than sending it. The answer is already known, and a
        # key with a quote welded to it is still most of a live credential —
        # there is no reason to put it on the wire to confirm what its shape
        # already says.
        return report

    try:
        import anthropic
    except ImportError:
        report.models_error = "The anthropic package is not installed."
        return report

    client = anthropic.AsyncAnthropic(api_key=raw)

    # Ask the account what it can reach, so a failing model can be compared
    # against a real list rather than against what anyone remembers.
    try:
        listing = await client.models.list(limit=50)
        report.available_models = [m.id for m in listing.data]
    except Exception as exc:
        report.models_error = _explain(exc)
        return report

    for role, model in (
        ("reasoning", settings.model_reasoning),
        ("conversation", settings.model_conversation),
        ("fast", settings.model_fast),
    ):
        probe = ModelProbe(role=role, model=model)
        try:
            await client.messages.create(
                model=model,
                max_tokens=PROBE_MAX_TOKENS,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
            )
            probe.ok = True
        except Exception as exc:
            probe.error = _explain(exc)
        report.probes.append(probe)

    return report


def _explain(exc: Exception) -> str:
    """The vendor's own words, plus what to do about them.

    Deliberately not routed through `_translate_error`: that exists to *hide*
    this detail from a chat panel, and hiding it is the problem being solved
    here.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    message = str(exc)
    lowered = message.lower()

    if "credit balance" in lowered or "billing" in lowered:
        hint = "Add funds at console.anthropic.com/settings/billing."
    elif status == 404 or "not_found" in lowered or "model" in lowered and status == 400:
        hint = "That model id is not available to this account — see the list above."
    elif status in (401, 403):
        hint = "The key was rejected. Check it was pasted whole."
    elif status == 429:
        hint = "Rate limited; this is temporary."
    else:
        hint = ""

    detail = f"{name}"
    if status:
        detail += f" {status}"
    return f"{detail}: {message}" + (f"\n         → {hint}" if hint else "")


def render(report: ProviderReport) -> str:
    lines = ["", "JARVIS — provider check", ""]

    if report.key_present:
        lines.append(
            f"  Key: found in {report.key_source}, {report.key_length} chars, "
            f"ending {report.key_tail}"
        )
    else:
        lines.append("  Key: NOT FOUND")

    if report.available_models:
        lines.append("")
        lines.append("  Models this account can call:")
        lines.extend(f"    {model}" for model in report.available_models)

    if report.probes:
        lines.append("")
        lines.append("  Models JARVIS is configured to use:")
        lines.extend(probe.describe() for probe in report.probes)

    lines.extend(["", f"  {report.verdict}", ""])
    return "\n".join(lines)


def main() -> int:
    report = asyncio.run(check_provider())
    print(render(report))
    healthy = report.key_present and bool(report.probes) and all(
        p.ok for p in report.probes
    )
    return 0 if healthy else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
