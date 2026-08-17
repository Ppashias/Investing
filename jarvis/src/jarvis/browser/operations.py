"""What JARVIS can do to a page (Phase 4, Step 5).

Every Playwright call that touches page *content* lives here. The tools in
:mod:`jarvis.tools.builtin.browser_tools` contain none: they decide, and this
acts. Keeping the split means the security sequence is visible in one place and
the mechanics in another, and neither can quietly absorb the other.

## The one rule that makes the URL policy unskippable

:func:`navigate` does not take a URL. It takes a :class:`UrlDecision`, and
refuses one that is not ``ALLOWED``.

That is the whole design. A function taking a string would be correct only for
as long as every caller remembered to check first, and a caller that forgot
would look exactly like a caller that did not need to. Taking the decision
means the check has already happened by construction — there is no argument you
can build without running it.

The same shape applies to interaction: :func:`click` and :func:`fill` take an
:class:`ElementEntry` resolved from the registry, never a selector and never a
coordinate, so "which element" was settled by an inspection that found it.

## Redirects are checked after the fact, because they can only be

A server decides where a request ends up. The policy runs again on the final
URL, and a navigation that landed somewhere refused is reported as a redirect
violation with the page left where it is — the content is not read, not
extracted, and not returned.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from jarvis.browser.capabilities import BrowserError
from jarvis.browser.elements import ElementEntry, ElementRef, ElementRegistry
from jarvis.browser.policy import (
    FIELD_INSPECTION_JS,
    inspection_from_dom,
    refuse_if_credential,
)
from jarvis.browser.service import PageHandle
from jarvis.browser.urls import UrlDecision, UrlPolicy, UrlVerdict
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Roles worth offering the model. Deliberately short: these are the things a
#: page is *operated* through. Headings and paragraphs are content and belong
#: in :func:`extract`, not in a list of things to click.
INTERACTIVE_ROLES = (
    "button", "link", "textbox", "checkbox", "radio", "combobox", "searchbox",
)

#: Caps on what inspection returns. A page can have thousands of elements and
#: a model reading all of them learns less than one reading the first few
#: dozen — and pays for the difference in context it cannot spend on the task.
MAX_ELEMENTS = 60
MAX_TEXT_CHARS = 20_000


class NavigationFailed(BrowserError):
    """The page could not be loaded."""

    code = "browser_navigation_failed"


class RedirectRefused(BrowserError):
    """The navigation ended somewhere the policy refuses."""

    code = "browser_redirect_refused"


@dataclass(slots=True)
class NavigationResult:
    url: str
    title: str
    status: int | None = None
    redirected: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "status": self.status,
            "redirected": self.redirected,
        }


@dataclass(slots=True)
class InspectedElement:
    ref: ElementRef
    role: str
    name: str
    description: str
    #: For inputs: whether JARVIS would refuse to type here, and why. Surfaced
    #: at *inspection* time rather than only on refusal, so the model can route
    #: around a login form instead of proposing a fill that will be rejected.
    credential: str | None = None

    def describe(self) -> dict[str, Any]:
        payload = {
            "element_id": self.ref.element_id,
            "role": self.role,
            "name": self.name,
        }
        if self.credential:
            payload["refuses_input_because"] = self.credential
        return payload


@dataclass(slots=True)
class PageInspection:
    url: str
    title: str
    elements: list[InspectedElement] = field(default_factory=list)
    truncated: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "truncated": self.truncated,
            "elements": [e.describe() for e in self.elements],
        }


async def navigate(
    handle: PageHandle,
    decision: UrlDecision,
    *,
    url_policy: UrlPolicy,
    timeout_seconds: float,
) -> NavigationResult:
    """Load an already-approved destination.

    Takes the decision rather than the URL. See the module docstring: this is
    the mechanism that makes skipping the policy impossible rather than merely
    discouraged.
    """
    if not decision.allowed:
        raise BrowserError(
            "navigate() was given a URL decision that is not ALLOWED "
            f"({decision.verdict.value}). This is a programming error: the "
            "policy must be consulted before navigation, not after."
        )

    handle.blocked = None
    try:
        response = await _goto(handle, decision.url, timeout_seconds=timeout_seconds)
    except Exception as exc:
        # The context guard aborts a refused destination before it is
        # dispatched, and Chromium then reports the navigation as a transport
        # failure. Without this, a policy refusal would surface as "could not
        # load" — true, but useless, and indistinguishable from the site being
        # down. The guard records what it refused; say that instead.
        blocked = handle.blocked
        if blocked is not None:
            # Deliberately left set. It is not only this call's explanation —
            # it is the record that this page's last navigation was refused,
            # which is what keeps the page inert until something navigates it
            # somewhere permitted. Cleared above, at the start of the next
            # attempt, rather than here.
            raise RedirectRefused(blocked.reason, blocked.reason) from exc
        raise NavigationFailed(
            f"Could not load {decision.url}: {exc}",
            f"I could not load {decision.url}.",
        ) from exc

    if handle.blocked is not None:
        # A refusal normally makes ``goto`` raise, so this is the belt to that
        # brace: a navigation reported as succeeding while the guard was
        # refusing part of it must not come back as a success. Reporting the
        # page as loaded here is the one outcome worth ruling out twice.
        raise RedirectRefused(handle.blocked.reason, handle.blocked.reason)

    final_url = handle.page.url
    redirected = _differs(final_url, decision.url)
    if redirected:
        # The server chose where this ended up, so the policy runs again on
        # what it chose. Nothing is read from the page before this passes.
        landed = url_policy.check_redirect(final_url, from_url=decision.url)
        if not landed.allowed:
            raise RedirectRefused(landed.reason, landed.reason)

    return NavigationResult(
        url=final_url,
        title=await handle.page.title(),
        status=getattr(response, "status", None),
        redirected=redirected,
    )


#: How long to keep re-issuing a navigation that a superseding one interrupted,
#: and how long to wait between attempts. Small: the thing being waited for is
#: a page commit that has already started.
_SUPERSEDED_BUDGET_SECONDS = 1.0
_SUPERSEDED_PAUSE_SECONDS = 0.05


async def _goto(handle: PageHandle, url: str, *, timeout_seconds: float) -> Any:
    """``page.goto``, retried while an in-flight navigation keeps superseding it.

    Needed because a refused navigation is *aborted*, and Chromium responds by
    committing its own error page. That commit is itself a navigation, so a
    request to leave the refused page — the documented way to recover one —
    arrives while it is still in progress and is rejected with "interrupted by
    another navigation". Measured at 8 failures in 12 attempts, so this is the
    ordinary path rather than a rare race.

    Waiting for a load state does not help: the *previous* page is already
    loaded, so the wait returns immediately. What has to be waited on is the
    error page taking over, which has no event this layer can name.

    Retrying is safe by construction. Every attempt is a fresh request through
    the context guard, so no attempt escapes the policy; and an attempt refused
    by the guard sets ``handle.blocked``, which stops the loop rather than
    re-issuing anything.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SUPERSEDED_BUDGET_SECONDS
    while True:
        try:
            return await handle.page.goto(
                url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded"
            )
        except Exception as exc:
            superseded = "interrupted by another navigation" in str(exc)
            if (
                not superseded
                or handle.blocked is not None
                or loop.time() >= deadline
            ):
                raise
            log.debug("browser_navigation_superseded", url=url)
            await asyncio.sleep(_SUPERSEDED_PAUSE_SECONDS)


def _differs(final: str, requested: str) -> bool:
    """Did the server send us somewhere else?

    Fragment-insensitive: ``#section`` is resolved by the browser and never
    reaches the server, so treating it as a redirect would re-check a
    navigation that did not happen.
    """
    return final.split("#", 1)[0].rstrip("/") != requested.split("#", 1)[0].rstrip("/")


async def inspect(
    handle: PageHandle,
    registry: ElementRegistry,
    *,
    limit: int = MAX_ELEMENTS,
) -> PageInspection:
    """List what the page can be operated through, and issue references.

    Bounded and structured rather than a DOM dump. A model handed the whole
    tree spends its context on markup; what it needs is "here are the things
    you can press, and here is what they are called".

    Every element listed gets a registry reference. That is the only way a
    later action can name it — no selector crosses this boundary, so the model
    cannot compose one for an element nobody looked at.
    """
    page = handle.page
    elements: list[InspectedElement] = []
    truncated = False

    for role in INTERACTIVE_ROLES:
        if len(elements) >= limit:
            truncated = True
            break
        locator = page.get_by_role(role)
        try:
            count = await locator.count()
        except Exception as exc:  # pragma: no cover - a page that died mid-scan
            log.debug("browser_inspect_role_failed", role=role, error=str(exc))
            continue

        for index in range(count):
            if len(elements) >= limit:
                truncated = True
                break
            item = locator.nth(index)
            try:
                name = (await item.get_attribute("aria-label")) or ""
                if not name:
                    name = ((await item.inner_text()) or "").strip()
                if not name:
                    name = (await item.get_attribute("placeholder")) or ""
                if not name:
                    name = (await item.get_attribute("name")) or ""
            except Exception:  # pragma: no cover - element vanished mid-scan
                continue
            name = " ".join(name.split())[:120]

            credential = None
            if role in ("textbox", "searchbox", "combobox"):
                try:
                    payload = await item.evaluate(FIELD_INSPECTION_JS)
                    from jarvis.browser.policy import credential_reason

                    credential = credential_reason(inspection_from_dom(payload))
                except Exception:  # pragma: no cover
                    # Unreadable: inspection_from_dom fails closed, so this
                    # reports as a credential field rather than a safe one.
                    from jarvis.browser.policy import credential_reason

                    credential = credential_reason(inspection_from_dom(None))

            href = ""
            if role == "link":
                # Where it goes, so the human approving a click can be told.
                # Best effort: a link whose target cannot be read is still a
                # link, and the confirmation simply says less about it.
                try:
                    href = (await item.get_attribute("href")) or ""
                except Exception:  # pragma: no cover - element vanished
                    href = ""

            description = f"the {role}" + (f" “{name}”" if name else "")
            ref = registry.register(
                page_id=handle.page_id,
                locator=item,
                description=description,
                role=role,
                name=name,
                href=href,
            )
            elements.append(
                InspectedElement(
                    ref=ref, role=role, name=name, description=description,
                    credential=credential,
                )
            )

    return PageInspection(
        url=page.url,
        title=await page.title(),
        elements=elements,
        truncated=truncated,
    )


async def extract(handle: PageHandle, *, limit: int = MAX_TEXT_CHARS) -> str:
    """The page's visible text.

    ``innerText`` rather than the HTML: the caller wants what a person would
    read, and markup is both larger and an invitation to reason about
    structure the model cannot act on anyway.
    """
    try:
        text = await handle.page.locator("body").inner_text()
    except Exception as exc:
        raise BrowserError(
            f"Could not read the page: {exc}", "I could not read that page."
        ) from exc
    text = (text or "").strip()
    return text[:limit]


async def summarise(handles: list[PageHandle]) -> list[dict[str, Any]]:
    """Id, address and title for each open page.

    Lives here rather than in the tool for the same reason everything else
    does: ``page.title()`` is a Playwright call, and the tool layer makes none.

    A title is page-authored text — untrusted, like any other string a site
    supplies — and it is also the only human-readable handle on a list of
    otherwise identical page ids. Fetching it must not be able to break the
    listing, so a page that will not answer is reported without one rather than
    failing the whole call.
    """
    rows: list[dict[str, Any]] = []
    for handle in handles:
        try:
            title = await handle.page.title()
        except Exception:
            title = ""
        rows.append({"page_id": handle.page_id, "url": handle.page.url,
                     "title": title})
    return rows


async def click(entry: ElementEntry, *, timeout_seconds: float) -> str:
    """Click the referenced element, and nothing else.

    No coordinates, no fallback. If the locator cannot be resolved the action
    fails — which is the point. A fallback that clicked "near enough" would
    report success for having pressed something nobody chose.
    """
    try:
        await entry.locator.click(timeout=timeout_seconds * 1000)
    except Exception as exc:
        raise BrowserError(
            f"Could not click {entry.description}: {exc}",
            f"I could not click {entry.description}.",
        ) from exc
    return entry.description


async def fill(entry: ElementEntry, text: str, *, timeout_seconds: float) -> str:
    """Type into the referenced element, unless it asks for a secret.

    The credential check runs here — against the live DOM, immediately before
    typing — rather than at inspection time only. Inspection may have happened
    many steps ago, and a field's type can change; the check that matters is
    the one closest to the keystroke.
    """
    try:
        payload = await entry.locator.evaluate(FIELD_INSPECTION_JS)
    except Exception as exc:
        raise BrowserError(
            f"Could not examine {entry.description} before typing: {exc}",
            f"I could not examine {entry.description} before typing into it.",
        ) from exc

    # Raises CredentialRefused. Deliberately before the fill, and deliberately
    # not conditional on anything the model said.
    refuse_if_credential(inspection_from_dom(payload), target=entry.description)

    try:
        await entry.locator.fill(text, timeout=timeout_seconds * 1000)
    except Exception as exc:
        raise BrowserError(
            f"Could not type into {entry.description}: {exc}",
            f"I could not type into {entry.description}.",
        ) from exc
    return entry.description
