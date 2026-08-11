"""How a browser action names what it acts on (Phase 4, Step 4D).

The Phase 4 decision document rules out coordinates, and the reason is not
ergonomics. ``click(840, 312)`` clicks whatever is at that point when the click
lands. If the page reflowed, an advert loaded, or a cookie banner pushed the
layout down, it clicks something else *and reports success* — the fake-success
failure the whole system is built to avoid, reintroduced at the lowest possible
level. It also makes the confirmation dialog unreadable: nobody can approve
"click at (840, 312)", but anyone can approve "click the Transfer funds
button".

So actions name elements. This module is how.

## Why not just pass a selector

A CSS selector is a *query*, not an identity. Handing ``#submit`` back and
forth means re-running the query at action time against whatever the page has
become, which has the coordinate problem again with extra steps: the selector
still matches, it just matches something else. It is also a string the model
composes, so a model that has never inspected a page can invent one and act on
an element nobody looked at.

A reference here is issued by the registry, in response to an inspection that
actually found the element. The model cannot mint one. It can *say* anything,
but saying ``el_7`` when no ``el_7`` was issued fails a lookup, and lookup —
not parsing — is what validates.

## Three ways a reference dies

**Its page closed.** The entry goes with it.

**Its page navigated.** A reference means "the third button on the page I
showed you". After a navigation that page is gone even though the ``Page``
object persists, and every reference into it is meaningless. Each page carries
a generation that increments on main-frame navigation, and a reference stamped
with an older generation is refused. This is the case a selector cannot
express: ``#submit`` still resolves after navigation, to a different button.

**Its browser died.** Handled one level up, by the service dropping pages.

## Bounded, because a long turn should not become a leak

Each entry holds a Playwright locator, which holds the page. Entries are
dropped when the page navigates, when it closes, and when the browser goes; a
per-page cap evicts the oldest beyond it, so a run that inspects a page fifty
times keeps the last ``max_elements`` rather than all fifty. None of this is
speculative: the registry is per-service and lives as long as the browser does.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from jarvis.browser.capabilities import BrowserError
from jarvis.db.base import new_id
from jarvis.logging import get_logger

log = get_logger(__name__)


class ElementReferenceError(BrowserError):
    """Base for every way a reference can fail to resolve."""

    code = "browser_element_reference_invalid"


class UnknownElement(ElementReferenceError):
    """No such reference was ever issued, or it has been evicted."""

    code = "browser_element_unknown"


class StaleElement(ElementReferenceError):
    """Issued for an earlier state of the page."""

    code = "browser_element_stale"


class WrongPage(ElementReferenceError):
    """Issued for a different page than the one being acted on."""

    code = "browser_element_wrong_page"


@dataclass(frozen=True, slots=True)
class ElementRef:
    """What a tool passes back to act on something it inspected.

    Carries its page and generation so a mismatch is caught before the action
    rather than after it. The id is opaque and issued by the registry: it is
    checked by lookup, never by parsing, so composing a plausible-looking one
    achieves nothing.
    """

    element_id: str
    page_id: str
    generation: int

    def describe(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "page_id": self.page_id,
            "generation": self.generation,
        }


@dataclass(slots=True)
class ElementEntry:
    """A live reference: how to reach the element, and how to describe it."""

    ref: ElementRef
    #: A Playwright ``Locator``. Typed loosely so this module imports where
    #: Playwright does not.
    locator: Any
    #: Human-readable, for confirmations and the audit log. "the Save button",
    #: never "(840, 312)".
    description: str
    role: str = ""
    name: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            **self.ref.describe(),
            "description": self.description,
            "role": self.role,
            "name": self.name,
        }


@dataclass(slots=True)
class _PageElements:
    generation: int = 0
    entries: "OrderedDict[str, ElementEntry]" = field(default_factory=OrderedDict)


class ElementRegistry:
    """Page-scoped, generation-stamped element references.

    One per :class:`~jarvis.browser.service.BrowserService`. Holds no browser
    state of its own beyond the locators it was handed, and is emptied by the
    same lifecycle events that empty the service.
    """

    def __init__(self, *, max_elements: int = 200) -> None:
        #: Per page. A cap rather than unbounded growth: entries hold locators,
        #: locators hold the page, and a long turn that re-inspects repeatedly
        #: should keep the recent ones rather than every one.
        self.max_elements = max_elements
        self._pages: dict[str, _PageElements] = {}

    # ── issuing ──────────────────────────────────────────────────────────────

    def generation(self, page_id: str) -> int:
        return self._pages.setdefault(page_id, _PageElements()).generation

    def register(
        self,
        *,
        page_id: str,
        locator: Any,
        description: str,
        role: str = "",
        name: str = "",
    ) -> ElementRef:
        """Issue a reference for an element that was actually found."""
        page = self._pages.setdefault(page_id, _PageElements())
        ref = ElementRef(
            element_id=new_id("el"), page_id=page_id, generation=page.generation
        )
        page.entries[ref.element_id] = ElementEntry(
            ref=ref, locator=locator, description=description, role=role, name=name
        )
        while len(page.entries) > self.max_elements:
            evicted, _ = page.entries.popitem(last=False)
            log.debug("browser_element_evicted", page_id=page_id, element=evicted)
        return ref

    # ── resolving ────────────────────────────────────────────────────────────

    def resolve(self, ref: ElementRef, *, page_id: str) -> ElementEntry:
        """Return the entry, or raise saying precisely what is wrong.

        ``page_id`` is the page the caller is about to act on, passed
        separately and compared against the reference's own. They differ
        exactly when a reference is being used against the wrong page, which is
        the case that must never silently work: the same reference resolving on
        two pages would mean "the third button" acting on a page nobody looked
        at.
        """
        if ref.page_id != page_id:
            raise WrongPage(
                f"Element {ref.element_id} belongs to page {ref.page_id}, not "
                f"{page_id}. An element reference cannot be used on a "
                "different page — inspect the page you mean and use the "
                "references it returns."
            )

        page = self._pages.get(page_id)
        if page is None:
            raise UnknownElement(
                f"Page {page_id} has no element references. Inspect it first."
            )

        if ref.generation != page.generation:
            raise StaleElement(
                f"Element {ref.element_id} was found on an earlier version of "
                f"{page_id} (generation {ref.generation}, now "
                f"{page.generation}). The page has navigated since; inspect it "
                "again and use the new references."
            )

        entry = page.entries.get(ref.element_id)
        if entry is None:
            raise UnknownElement(
                f"There is no element {ref.element_id} on {page_id}. It was "
                "never issued, or it has been dropped."
            )
        return entry

    def entries_for(self, page_id: str) -> list[ElementEntry]:
        page = self._pages.get(page_id)
        return list(page.entries.values()) if page else []

    def count(self, page_id: str) -> int:
        page = self._pages.get(page_id)
        return len(page.entries) if page else 0

    # ── invalidating ─────────────────────────────────────────────────────────

    def page_navigated(self, page_id: str) -> int:
        """Bump the generation and drop every reference into the old page.

        Both halves matter. The bump is what makes an old reference *detectably*
        stale rather than silently resolving to whatever now occupies the same
        selector; the drop is what stops the locators keeping the previous
        page's objects alive for the rest of the session.
        """
        page = self._pages.setdefault(page_id, _PageElements())
        dropped = len(page.entries)
        page.entries.clear()
        page.generation += 1
        if dropped:
            log.debug(
                "browser_elements_invalidated_by_navigation",
                page_id=page_id, dropped=dropped, generation=page.generation,
            )
        return page.generation

    def forget_page(self, page_id: str) -> None:
        """Drop a closed page entirely."""
        if self._pages.pop(page_id, None) is not None:
            log.debug("browser_elements_forgotten", page_id=page_id)

    def clear(self) -> None:
        """Drop everything. Called when the browser goes."""
        self._pages.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "pages": {
                page_id: {
                    "generation": page.generation,
                    "elements": len(page.entries),
                }
                for page_id, page in self._pages.items()
            },
            "max_elements": self.max_elements,
        }
