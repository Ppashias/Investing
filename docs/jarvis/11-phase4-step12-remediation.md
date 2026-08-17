# Phase 4, Step 12 — targeted security remediation

Fixes the HIGH finding and the two MEDIUM findings from
[`10-phase4-final-audit.md`](10-phase4-final-audit.md). The two INFORMATIONAL
notes in that report (`extras["browser"]` not structurally pinned, the shared
cookie jar) are deliberately untouched, as are every one of the carried-forward
known limitations.

---

## 1. HIGH — a click could reach a destination the URL policy refuses

### Where the boundary actually was

`UrlPolicy` guarded two functions: `browser_open` and `browser_navigate`. That
is a guard on *the two places JARVIS asks to navigate*, and the thing being
guarded is *navigation*, which is not the same set. A click on a link, a
`<meta refresh>`, a script assigning `location`, a form submission and a server
redirect all navigate, and none of them goes through either tool.

So the repair is not in `operations.click()`. Checking the href before clicking
would close the one case the audit demonstrated and leave the other five open,
and it would put a copy of the SSRF boundary in a function whose job is to
press a button. The layer that sees every navigation, whatever caused it, is
the browser context.

```python
await self._context.route("**/*", self._guard_navigation)
```

One route handler on the one `BrowserContext` the service owns. It aborts a
document request whose destination the policy refuses, **before the request is
dispatched** — the destination is never contacted, rather than contacted and
not read. That distinction is the whole point: the audit's containment analysis
established that the response body already could not reach the model, and a
request to `169.254.169.254` is an attack whether or not anyone reads the
answer.

Four properties worth stating:

- **No second policy.** The handler builds a `UrlPolicy` from
  `self.settings`, the same source the tools build theirs from. Rebuilt per
  request rather than captured at launch, so an operator changing
  `allow_localhost` cannot leave the guard disagreeing with the tools.
- **Documents only.** A page JARVIS is permitted to read fetches its own
  images and scripts from wherever it likes, exactly as it would in the user's
  browser. What this stops is the browser being *steered*. Scoping to
  `resource_type == "document"` also keeps `UrlPolicy.check` — which resolves
  DNS, synchronously — off the path of every image on every page.
- **Fails closed.** An error deciding a destination aborts the navigation.
- **The refusal is explained.** Chromium reports an aborted navigation as a
  generic transport error, so a refusal would otherwise surface as "could not
  load" — true, useless, and indistinguishable from the site being down. The
  guard records the decision on the page it was aimed at, and the navigation
  and click paths report *that* instead.

### The second leg: server redirects

Checking where a click is *aimed* is not sufficient, and this was measured
rather than predicted. With the guard in place but checking only the initial
request, a click on a permitted same-origin link whose server answered `302
Location: http://192.0.2.2:.../via-redirect` reached that address:

```
[diag] routed requests:
    ('document', 'http://127.0.0.1:43743/link?to=...', False)
    ('document', 'http://127.0.0.1:43743/redirect-to?to=...', False)
[diag] VICTIM HITS: ['/via-redirect']
```

The handler was never called for the third URL. Chromium follows a redirect
chosen by the server inside the network stack, without consulting the route
handler again.

So the guard takes the hop itself. A permitted document request is issued with
redirects disabled; a `3xx` is checked against `UrlPolicy.check_redirect`
before anything else happens, and only then handed back to Chromium — which
re-requests the new location and arrives at this handler again. Every hop is
therefore checked before it is dispatched.

The cost is real and is stated rather than hidden: every *document* load now
goes through Playwright's fetch rather than Chromium's. Sub-resources are
untouched.

### Proof

The Step 11 exploit is now
`test_clicking_a_link_to_a_refused_address_issues_no_request`, and it asserts
against the victim server's own access log rather than against JARVIS's prose.
Re-running the original out-of-repo probe unchanged:

```
click #2 -> is_error=True
PAGE URL AFTER CLICK: chrome-error://chromewebdata/
>>> VICTIM HITS: []
```

Ten tests cover the invariant, and they are about *causes* of navigation
rather than about `click()`:

| Test | What it proves |
|---|---|
| `test_a_click_that_navigates_within_policy_still_works` | the guard does not break ordinary browsing |
| `test_submitting_a_form_still_posts_its_body` | fetch-and-fulfill preserves method and body |
| `test_clicking_a_link_to_a_refused_address_issues_no_request` | the Step 11 exploit — victim log empty |
| `test_clicking_a_link_to_the_metadata_endpoint_issues_no_request` | the destination that makes it an attack |
| `test_clicking_a_link_that_redirects_out_of_policy_issues_no_request` | the redirect leg — victim log empty |
| `test_clicking_a_link_that_redirects_to_the_metadata_endpoint_is_stopped` | the same, aimed at metadata |
| `test_a_page_that_navigates_itself_out_of_policy_is_stopped` | script-driven `location` — no tool involved at all |
| `test_a_refused_click_is_audited_as_a_refusal` | the attempt is in the activity log with its verdict |
| `test_a_page_left_on_a_refused_destination_becomes_inert` (existing) | the page is inert afterwards |
| `test_a_refused_page_can_always_be_navigated_back` | recovery works every time, not most of the time |
| `test_the_only_routing_in_the_subsystem_is_the_navigation_guard` | there is exactly one route handler, and it is this one |

A refused click reports `is_error=True` with `blocked_navigation=True` rather
than success. Reporting "Clicked the link" for a navigation that was refused
would be the fake-success failure this subsystem is built to avoid.

### Two behaviour changes this forced

#### Recovering from a refusal

Aborting a navigation makes Chromium commit its own error page, and that
commit is itself a navigation. The documented way to recover a refused page —
navigate it somewhere permitted — therefore arrived while the error page was
still taking over and was rejected outright:

```
Page.goto: Navigation to "http://127.0.0.1:32963/" is interrupted by
another navigation to "chrome-error://chromewebdata/"
```

Eight failures in twelve attempts. The ordinary path, not a rare race, and it
first surfaced as a single failing test in one randomised full-suite run —
which is exactly the shape of defect a single-round test passes by luck.

Waiting for a load state does not help: the previous page is already loaded,
so the wait returns immediately. `operations._goto` retries while the error
says the navigation was superseded, bounded to one second. Retrying is safe by
construction — every attempt is a fresh request through the guard, and an
attempt the guard refuses sets `handle.blocked`, which stops the loop rather
than re-issuing anything. `test_a_refused_page_can_always_be_navigated_back`
runs five rounds; 12/12 recover where 4/12 did before.

Fulfilling a synthetic refusal page instead of aborting would have avoided the
race, and was rejected: it would make the *document origin* the refused one,
and since sub-resources are deliberately not guarded, that would hand a page at
`http://169.254.169.254/` a same-origin fetch. Abort is the honest primitive;
the race is ours to handle.

#### `_origin_of` no longer trusts `page.url` alone

`_origin_of` re-checks `page.url` on every tool call, which is what makes a
page sitting on a refused destination inert. The guard aborts *before*
Chromium commits anything, so for a short window the page still reports the
address it was on when the refused navigation started — permitted, and about
to be replaced by an error page. Measured:

```
t+0.0s url=http://127.0.0.1:44645/     extract is_error=False  ← the window
t+0.3s url=chrome-error://chromewebdata/  extract is_error=True
```

`page.url` cannot answer "was this page's last navigation refused?", so it is
no longer asked to. The guard's record on the handle is checked first, and is
cleared by the next deliberate navigation rather than by the call that reports
it — so a page whose last move was refused stays inert until it is moved
somewhere permitted or closed. Deterministic, with no dependence on when
Chromium commits.

---

## 2. MEDIUM — the confirmation was unreadable

`06-phase4-browser-control-decision.md` §4.3 argued for element handles over
coordinates partly because *"'Click at (840, 312)' is not something a human can
meaningfully approve."* The implementation asked people to approve
`pg_08c0…/el_d23c…`, which is the same problem with different characters. It
compounds finding 1 directly: the last control between a poisoned page and an
out-of-policy request is a human saying yes, and that human was shown an id.

**What is now displayed, and why each part is safe to display:**

| Shown | Why it is safe |
|---|---|
| the element's **role** and **accessible name** | the page's own labelling, already returned verbatim to the model by `browser_inspect` |
| the page's **current address** | already in `browser_pages` output and in every audit row for the action |
| for a link, its **destination**, resolved against the page | the single most decision-relevant fact, and the one whose absence made a link click unreviewable |
| for a fill, the **text being typed** | unchanged from before; approving "type X" requires seeing X |

Nothing reads a cookie, a storage entry, a header or a field value. Credential
fields are refused before any keystroke, and the fill value remains redacted
everywhere it is *stored* (`redact_arguments`, `stored_arguments`).

> Click the link “Continue” on http://127.0.0.1:41003/link?to=…
>
> The page says this link leads to http://192.0.2.2:44975/stolen — that is the
> page's own claim about itself.

The name and the href are page-authored, so they are whitespace-collapsed,
length-capped and attributed to the page rather than stated as fact. Collapsing
whitespace is not cosmetic: newlines would let a page write its own extra lines
into the question a human is reading.

**What did not change.** The confirmation fingerprint is still computed over
`(tool name, arguments)`, so an approval is still single-use and still bound to
this exact page, element and text — asserted by
`test_the_approval_is_still_bound_to_the_exact_call`, which approves a click on
one link and shows that a click on the other still asks. Destination and element
binding are untouched. A tool that cannot describe its target falls back to the
old template rather than failing to produce a confirmation at all: describing
must never be able to block approving.

The mechanism is `Tool.describe_confirmation`, an optional hook given the
arguments *and* the live context, because the meaning of a handle lives in a
registry the executor cannot reach and must not have to know about.

---

## 3. MEDIUM — a navigation listener that failed open

```python
except Exception:   # pragma: no cover
    return          # ← returns WITHOUT invalidating
```

If `frame.parent_frame` raised, element references into a page that may have
been replaced stayed valid. A locator is a lazily resolved selector, so a
surviving reference resolves against the *new* DOM and matches whatever now
sits where the old element was — and acting on it is reported as a success.

Unknown frame state now invalidates. The only reason to keep references is
positive knowledge that this was a sub-frame; anything else throws them away.
Losing references costs an extra inspection, keeping the wrong ones costs the
guarantee.

The closure became `BrowserService._frame_navigated`, a named method, so the
branch could be exercised rather than only reasoned about.
`test_an_unreadable_frame_state_invalidates_the_page` calls the handler the
event calls with a frame whose `parent_frame` raises, and asserts the
generation bumped and the entries went.

**This is a claim about the handler, not about Chromium.** No way was found to
make Chromium produce that state; the test proves the unknown-state branch
fails safe, not that the branch is reachable at runtime. The audit's
"reachability unproven" caveat still stands and is not being quietly retired.
`test_a_sub_frame_navigating_leaves_the_page_alone` pins the other half, or the
fix would just be "always invalidate", which would make references unusable on
any page carrying an iframe.

---

## 4. Two existing guard-rail tests were narrowed, not weakened

Both asserted the *absence* of something Step 12 deliberately adds.

- `test_nothing_in_the_subsystem_navigates_yet` banned `route(` in six
  modules. `service` is now allowed it, and the exception is immediately
  re-pinned by a new test asserting there is exactly one registration, that it
  is `("**/*", self._guard_navigation)`, and that the ways a routed request can
  end are countable (two aborts, one pass-through, one fetch-and-fulfill).
  The five other modules and every other banned token are unchanged.
- `test_no_credential_handling_exists_in_the_runtime` banned `fill(`, which
  matched `route.fulfill(` as a substring. Now `.fill(`. `locator.fill(...)` is
  still banned in every module the test covers, which is what the rule meant.

---

## 5. Scope

**Fixed:** the HIGH finding (both legs), and both MEDIUM findings.

**Deliberately not touched:** `extras["browser"]` visibility, the shared cookie
jar, cross-turn taint, `Confirmation.body` retaining the pending value,
`ActivityService.record` swallowing DB errors, the browser `_audit` contract,
shared `Tool` singleton test-order pollution, SSE socket/transport coverage,
thin `cross_page` mutation coverage, and browser-suite load sensitivity.

**Windows: UNVERIFIED.** Nothing was executed on Windows. The added code is
`urllib.parse` and Playwright routing, with no paths, signals or subprocesses,
but that is source analysis and not a Windows claim.
