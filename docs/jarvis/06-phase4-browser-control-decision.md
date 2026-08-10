# Phase 4 — browser control: the architectural decision

**Status:** decision recorded, nothing implemented. No code in this repository
depends on anything below. Written during the pre-Phase-4 hardening pass so
that Phase 4 starts from a decision rather than from a default.

---

## 1. The problem Phase 3 leaves behind

Phase 3 built real computer control on a real backend — X11, via python-xlib
and XTEST. On a Linux machine with a display, or a headless one with Xvfb, it
observes the screen, enumerates windows, moves the pointer and types. That
works, it is tested, and none of it is in question here.

It also does not run on Windows. Not "runs badly" — does not run. X11
automation drives an X server, and a Windows desktop is not one. The hardening
pass made JARVIS say so honestly (`docs/jarvis/` commit *"tell the truth about
what this machine can do"*): the Windows path reports no display, refuses every
desktop action with a reason that names the missing backend rather than a
missing package, and PlanStage withholds the tools entirely so the model is
never offered a click it cannot perform.

Honest is not the same as useful. The user runs JARVIS on Windows. So the
question Phase 4 has to answer is not "how do we polish computer control" but
**"what can JARVIS actually drive on the machine the user actually has?"**

## 2. Three options, and why the answer is a browser

**Implement a Windows desktop backend.** UIAutomation for the accessibility
tree, SendInput for synthetic input, DXGI or PrintWindow for capture. This is
the direct answer and it is the wrong first move. It is a large amount of
platform-specific code with no shared surface with the X11 backend beyond the
`Backend` interface; it needs per-application quirk handling almost
immediately; and it grants JARVIS the ability to click anything on the user's
machine, which is the widest possible blast radius to open in a single phase.
It is a legitimate Phase 5+ project. It is not where to start.

**Wait for a cross-platform desktop library.** There isn't one that is both
maintained and honest about what it does. Deferring is not a decision.

**Drive a browser instead.** Most of what "control my computer" means in
practice — look something up, fill this in, read that dashboard, download the
statement — happens in a browser. A browser is the one application that
behaves identically on Windows, macOS and Linux, exposes a documented
automation protocol, and can be given its own instance and its own profile.

The third is the right first move, and the reason is not convenience. It is
that a browser lets JARVIS act on the user's machine **without JARVIS being
able to act on everything on the user's machine.** The bound is structural.

## 3. Playwright over CDP, and why not raw CDP

Playwright is the mechanism. It speaks the Chrome DevTools Protocol underneath
and adds three things that matter more than the protocol does:

- **Auto-waiting.** Playwright waits for an element to be attached, visible,
  stable and enabled before acting, and fails with a description of which
  condition was not met. Raw CDP gives a race and a timeout.
- **One API, several engines.** Chromium, Firefox and WebKit behind the same
  calls. The commitment is to Chromium; the option costs nothing.
- **It is not a screen.** Everything below about element handles is only
  available because Playwright's model is the DOM, not pixels.

Raw CDP would mean reimplementing all of that. Selenium would mean the WebDriver
protocol and a slower, coarser API. Neither trade is worth taking.

**Nothing about this depends on X11.** Playwright launches and drives a browser
process through a pipe or a WebSocket; it does not need a display server, and
Chromium's headless mode does not need one either. That is the whole point:
Phase 3's backend cannot exist on Windows, and Phase 4's does not have to.

## 4. The four constraints, and why each is load-bearing

### 4.1 A dedicated browser instance

JARVIS launches its own Chromium. It never attaches to the user's running
browser.

Attaching would be easier and is not acceptable. The user's browser holds their
logged-in sessions for everything — email, bank, employer, everything with a
cookie. Attaching to it hands JARVIS all of that at once, and it does so
*invisibly*: there is no moment where the user grants access to a specific
site, because the access came with the process. It also means a JARVIS mistake
lands in the window the user is working in — closing their tab, submitting
their half-written form.

A separate instance makes the blast radius a thing that can be described. What
JARVIS can reach is what it navigated to and what its own profile holds.

### 4.2 An isolated browser context

Each JARVIS session gets a fresh Playwright browser context: its own cookie
jar, its own storage, its own cache.

Two reasons, and the second is the one that matters. The obvious one is
cleanliness — sessions do not leak into each other, and a task that logs in
somewhere does not leave that login lying around for the next one.

The real one is that **an isolated context is what makes credential handling
tractable at all.** If JARVIS's browser shares state with anything, then "what
is JARVIS authenticated to?" has no answer, and every later rule about
credentials is unenforceable because there is no boundary to enforce it on.

### 4.3 Element handles, never coordinates

Every action targets a DOM element via a Playwright locator — role, label,
text, or a selector. No action is expressed as a click at (x, y).

This is the constraint most likely to be relaxed under pressure, and the one
that should not be. Three reasons:

- **A coordinate can lie; a locator cannot.** `click(840, 312)` clicks whatever
  happens to be there. If the page reflowed, an advert loaded, or a banner
  pushed the layout down, it clicks something else and reports success. This is
  the fake-success failure mode the whole system is built to avoid, and pixel
  targeting reintroduces it at the lowest level.
- **The confirmation becomes readable.** "Click at (840, 312)" is not something
  a human can meaningfully approve. "Click the *Transfer funds* button" is. The
  permission and confirmation machinery already built assumes actions can be
  described; coordinates break that assumption.
- **The audit becomes readable for the same reason.** A log of coordinates
  answers no question anyone will ever ask.

Screenshots stay — for the user to look at, and for verification. They are not
a targeting mechanism.

### 4.4 No automatic credential entry

JARVIS never types a password, never fills a credential field from a store, and
never completes an authentication flow on its own. When a page needs a login,
the run stops and the user is asked to authenticate.

This is not a limitation to be engineered away later. An agent that can
authenticate as the user is an agent whose blast radius is *everything the user
can log into*, and it collapses the distinction between "JARVIS did this" and
"the user did this" — in the logs, and in any dispute about what happened.
Every existing rule in the system depends on that distinction holding.

It also removes the most valuable target. If JARVIS holds no credentials and
enters none, prompt injection from a web page cannot extract them, because they
are not there to extract.

## 5. What the existing architecture already provides

The point of writing this now is that Phase 4 is mostly wiring, not invention:

- **`ToolExecutor`** stays the single chokepoint. Browser tools go through it
  like every other tool — schema validation, permission decision, timeout,
  persisted attempt.
- **The permission engine** already has the concepts: capability, resource
  scope (which becomes origin — `browser:github.com`), risk level,
  irreversibility floor, taint escalation.
- **Taint is already threaded.** A page JARVIS reads is untrusted content, and
  the existing rule — untrusted content escalates every non-read capability to
  a confirmation — is exactly the prompt-injection defence a browsing agent
  needs. It does not need to be invented, only connected.
- **Confirmations** are already fingerprint-bound and single-use, so an
  approval to click one button cannot be replayed against another.
- **`CapabilityReport`** already models "this machine cannot do this, and here
  is why", so a machine with no Chromium reports that in the same shape as a
  machine with no display.
- **The activity log** already records tool calls, permission decisions and
  subject-specific actions; a `BROWSER_ACTION` kind sits alongside
  `OBSIDIAN_ACTION` and `COMPUTER_ACTION`.

## 6. What is deliberately not decided here

Tab and download policy, how far a single autonomous run may go before
re-confirming, whether the browser is per-session or long-lived, and what
happens to the X11 backend once a browser backend exists. Those are Phase 4's
to settle with code in front of them.

## 7. Not implemented

No Playwright dependency has been added, no browser tool exists, no
`BROWSER_ACTION` kind exists, and no test in this repository launches a
browser. This document is a decision, and the next phase is the user's call.
