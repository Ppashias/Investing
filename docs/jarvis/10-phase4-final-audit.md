# Phase 4, Step 11 — final adversarial audit

**Status:** report-only. No production code, no tests changed. Findings below
are recorded for a future remediation step; none was fixed here.

The question this audit asked was not "do the tests pass" — Step 10 established
that they do — but *"can I find a security or correctness failure the existing
tests would not necessarily expose?"* Two answers were yes.

---

## 1. Headline finding — clicking a link bypasses the URL policy

**Severity: HIGH. Proven by live probe, not inferred.**

`UrlPolicy` guards `browser_open` and `browser_navigate`. It does not guard
navigation caused by **clicking a link**, because a click is not a navigation
as far as the tool layer is concerned — `operations.click()` calls
`locator.click()` and returns. Chromium then navigates wherever the anchor
points, and no policy sees that destination.

### The experiment

Two servers: a *lure* on loopback (permitted) serving
`<a href="http://192.0.2.2:PORT/stolen">Continue</a>`, and a *victim* bound to
`192.0.2.2`, which `UrlPolicy` refuses as a private address. Everything ran
through the real path — registry → `ToolExecutor` → permission → confirmation →
handler.

```
CONTROL   browser_navigate → is_error=True  verdict=FORBIDDEN_DESTINATION
BYPASS    browser_click    → is_error=False
          PAGE URL AFTER CLICK: http://192.0.2.2:39621/stolen
          VICTIM SERVER HITS:   ['/stolen', '/favicon.ico']
```

The control proves the policy works at the intended entry point. The victim's
own access log proves the request was issued anyway.

### What still holds

Containment is real and worth stating precisely. After the click the page sits
on a forbidden URL, and `_origin_of` re-checks the current URL on every
subsequent tool call, so the follow-up `browser_extract` refused:

> *That page is somewhere I am not allowed to act: 192.0.2.2 … is a private
> network address.*

So **the response body never reaches the model.** What the attacker gets is
request issuance — a GET to an address the policy exists to keep JARVIS away
from, with whatever the context's cookies are, plus the observable
success/failure of the click itself.

### Why the tests miss it

The fixture site contains only same-origin links (`/form`, `/injection`).
Nothing in 345 browser tests clicks a link that navigates anywhere. The Step 9
mutation battery could not have caught it either: it removes mechanisms that
exist, and this is a mechanism that does not.

### Is it a hard stop?

**No, on the brief's own wording** — hard-stop A is "a browser action can reach
Playwright without passing through the intended permission/confirmation path",
and this click did pass through both. It is nonetheless *adjacent* to that
condition, because the conflict is real: Playwright's model lets one action
cause a navigation that the permission model represents as a separate,
never-taken decision. Flagged for the reader to weigh rather than settled here.

---

## 2. The confirmation a user is asked to approve is unreadable

**Severity: MEDIUM.**

`06-phase4-browser-control-decision.md` §4.3 argues for element handles over
coordinates partly on this ground:

> The confirmation becomes readable. "Click at (840, 312)" is not something a
> human can meaningfully approve. "Click the *Transfer funds* button" is.

The implementation does not deliver that. The template is:

```python
confirmation_template="Click an element on the open page ({page_id}/{element_id})."
```

and the probe captured what a user actually sees:

```
'Click an element on the open page (pg_08c0c0a230144842adf9a676/el_d23c77910dc94da4a5dc22a7).'
```

Two opaque identifiers — as unreadable as the coordinates the design rejected.
The registry holds a human description (`"the Continue link"`) and the tools
use it in audit rows, but the confirmation is created by `ToolExecutor` *before*
the handler runs, and the executor has only the raw arguments.

This compounds finding 1: the one control standing between a poisoned page and
an out-of-policy request is a human approval, and that human is shown an id
rather than a destination.

`browser_fill` has the same shape, though it at least shows the value being
typed: `Type 'x' into an element on the open page (pg_…/el_…)`.

---

## 3. A navigation listener that fails open

**Severity: MEDIUM. Reachability unproven.**

`service.py` invalidates element references on navigation via a `framenavigated`
listener:

```python
def _navigated(frame, _page_id=handle.page_id):
    try:
        if frame.parent_frame is not None:
            return              # a sub-frame; the page itself is unchanged
    except Exception:           # pragma: no cover - defensive
        return                  # ← returns WITHOUT invalidating
    self.elements.page_navigated(_page_id)
```

If `frame.parent_frame` raises, the handler returns and `page_navigated` is
never called — the page has moved and its element references stay valid. A
locator is a lazily-resolved selector, so a surviving reference can match a
*different* element on the new DOM: precisely the "clicks whatever happens to be
there" failure the architecture was built to prevent.

The safe default for an unknown frame state is to invalidate, not to skip.
Marked `pragma: no cover`, so it has never executed in any test, and I have not
demonstrated a way to make `parent_frame` raise — hence MEDIUM with reachability
unproven rather than HIGH.

---

## 4. What was checked and found sound

- **Permission bypasses.** Every Playwright-touching call site lives in
  `src/jarvis/browser/`. `BrowserService.new_page()` takes no URL and cannot
  navigate. Navigation exists only in `operations.navigate()`, which refuses a
  `UrlDecision` that is not ALLOWED. **PASS**, subject to finding 1.
- **Tool registration vs execution.** 9 registered = 9 declared, no aliases, no
  alternate dispatch, all handlers coroutines, `browser_status` correctly the
  only tool never withheld. **PASS**
- **Cross-context access.** There is exactly one `BrowserContext` per service —
  "context A reading context B" cannot happen because there is no B. Recovery
  after a browser death builds a fresh context. Worth stating plainly, since
  origin permissions might suggest otherwise: **all pages in a session share one
  cookie jar**; isolation is from the user's browser and between sessions, not
  between origins. **PASS**
- **Windows.** `src/jarvis/browser/` uses `pathlib` throughout, with no
  `os.kill`, no signals, no `subprocess`, no `shell=True`, no `os.sep`, no
  hardcoded POSIX paths; teardown delegates to Playwright, which is
  cross-platform. **SOURCE-ANALYZED — WINDOWS UNVERIFIED.** Nothing was executed
  on Windows and no Windows claim is made.

---

## 5. Silent exception swallowing

Twenty-two broad handlers were inventoried across
`browser/`, `tools/executor.py` and `activity/service.py`. Most re-raise as a
typed `BrowserError` or log and continue. Notable classifications:

| Location | Behaviour | Class |
|---|---|---|
| `operations.py:241` credential inspection | falls back to `inspection_from_dom(None)`, which reports *credential* | **SAFE — fails closed, deliberately** |
| `service.py:499` `_navigated` | returns without invalidating | **GENUINE DEFECT** (finding 3) |
| `operations.py:304` page title | `title = ""` | SAFE |
| `operations.py:213/230` inspect scan | logs, skips the element | SAFE |
| `service.py:552/637/663` close paths | logs; teardown continues | SAFE — deliberate, so one failure cannot strand a browser |
| `executor.py:483` finalise flush | returns on a broken session | SAFE BUT UNDER-DOCUMENTED — a failed error-record write is itself unrecorded |
| `activity/service.py:145` `record` | logs, returns `None` | **KNOWN LIMITATION — unchanged** |

---

## 6. Known limitations — carried forward, none fixed

Cross-turn taint · `Confirmation.body` retaining the pending value ·
`ActivityService.record` swallowing DB errors · browser `_audit` stricter than
that contract · shared `Tool` singleton test-order pollution · SSE
socket/transport untested · thin `cross_page` mutation coverage · browser-suite
load sensitivity.

**Cross-turn taint remains unfixed and is reported, not remediated.** The audit
found no *additional* cross-turn path and nothing that makes it worse than
already documented in `08-phase4-browser-tools.md` §21.

Two new informational notes:

- `ToolContext.extras["browser"]` is visible to **every** tool, not only browser
  tools. Today only `browser_tools.py` reads it (4 sites, verified), so there is
  no current bypass — but nothing pins that, and a future tool could reach
  `BrowserService` without `UrlPolicy` or `BrowserPolicy`. The same
  "true by inspection" gap Step 9 closed for HTTP routes.
- The confirmation body for `browser_fill` deliberately contains the typed
  value, which is the documented trade-off; combined with finding 2, the user
  sees *what* is typed but not *where*.
