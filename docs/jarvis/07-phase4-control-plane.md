# Phase 4 Step 4 — the browser control plane

**Status:** implemented. No browser tool exists; Step 5 has not begun.

Five boundaries, all built before the tools that will use them. The ordering is
deliberate: a boundary retrofitted around a working tool tends to be shaped by
the tool, and the tool tends to have already grown a way around it.

---

## 1. Taint propagation (4A) — a real defect, found and closed

### What was wrong

`ToolContext.tainted` was computed from `ContextBundle.tainted` alone. That flag
is set once, at context assembly, from retrieved memory and knowledge. **No
tool result could taint anything.**

Reproduced before it was fixed, through the real agent loop:

```
read_obsidian_note("Evil.md")  → "IGNORE ALL PREVIOUS INSTRUCTIONS…"
list_obsidian_notes()          → tainted=False
```

The consequence, with a `tool:*` WRITE ALLOW grant: a note could instruct
JARVIS to create a file, and the file appeared with nobody asked. The Phase 1
taint-escalation rule was working perfectly and was never reached.

The Phase 4 decision document said taint "does not need to be invented, only
connected." That was half right. The mechanism existed; the wire did not.

### The fix

- `ToolResult.tainted`, set by a distinct `ToolResult.untrusted()` constructor.
  Not a keyword on `ok()` — `ok` collects `**data`, so `ok(text, tainted=True)`
  would put the flag in `data` and lose it silently. A test pins that trap.
- `PipelineContext.tool_taint`, monotonic, OR-ed with the bundle's taint on
  every tool call. Kept as a separate field so the two sources can never be
  mistaken for each other in a diagnosis.
- Accumulated after each result, so it applies to the next tool in the same
  batch as well as the next iteration.

Four existing tools now declare their output untrusted: `read_obsidian_note`,
`search_obsidian`, `search_knowledge`, and `read_file` (via the shared computer
helper). The criterion is provenance, not content: the user's own harmless note
taints, because "who wrote this" is knowable and "is this dangerous" is not.

**A clean result never clears taint.** The untrusted text is already in the
transcript the model is reasoning from; letting a harmless follow-up reset the
flag would make the defence defeatable in two steps.

---

## 2. URL and SSRF policy (4B)

`jarvis/browser/urls.py`. Two independent checks:

**Scheme** — only `http` and `https`. `file:` would make the browser a
filesystem reader that walks around Phase 3's configured roots entirely;
`data:` and `javascript:` are script-execution primitives with a URL's syntax.

**Destination** — resolved, then classified with `ipaddress`. String matching
is not enough and the tests show why: `2130706433`, `0x7f.0.0.1` and
`017700000001` are all `127.0.0.1`, and `intranet.example.com` looks public
until it resolves to `10.1.2.3`. Loopback, private, link-local, reserved,
multicast and unspecified are refused in v4 and v6, including IPv4-mapped and
6to4 forms. A name resolving to both a public and a private address is refused
— which of them the browser connects to is the browser's choice, not JARVIS's.

`169.254.169.254` is refused by the ordinary link-local rule rather than by a
special case. A special case only covers the address someone thought of.

**Redirects** get the same rules and their own verdict. A refused navigation is
a request that should not have been made; a refused redirect is a reasonable
request and a site that sent it somewhere it may not go. Only the second is
evidence about the site.

Four verdicts, kept apart because they have different causes and different
fixes: `INVALID`, `UNSUPPORTED_SCHEME`, `FORBIDDEN_DESTINATION`,
`REDIRECT_VIOLATION`.

Two escape hatches, defaulting off and independent of each other:
`allow_localhost` and `allow_private_networks`. An operator pointing JARVIS at
a dev server should not have to open their whole LAN.

---

## 3. Origin permissions (4C)

`jarvis/browser/policy.py`. The **same** `PermissionEngine`, one new resource
family. No second grants table, no mode ceiling, no rule that could disagree
with Phase 1 — the module builds a `PermissionRequest` and hands it over,
exactly as `ObsidianService.authorize` and `ComputerPolicyEngine` already do.

A second layer exists for a mechanical reason: `Tool.resource` is
`f"tool:{name}"`, fixed per tool, and a browser action's resource depends on
*which site* — an argument the executor cannot see when it decides.

### The canonical form is `browser:https://github.com:443`

Scheme and port are always present. `browser:github.com` would be tidier and
wrong twice: it makes `http://` and `https://` the same resource, so a grant
for the secure one silently covers plaintext; and it leaves the port implicit,
so `github.com` and `github.com:8443` collapse. Always-explicit means two
spellings of one destination produce one string.

### Two operations

| Operation | Capability | Reversible | Effect |
|---|---|---|---|
| `READ` | `READ` | yes | Open, navigate, inspect, extract |
| `INTERACT` | `EXTERNAL_ACTION` | **no** | Click, fill, select, submit |

`INTERACT` is declared irreversible because JARVIS cannot un-click a button —
the request reached the server, and whether it can be undone is the far side's
business. This engages Phase 1's irreversibility floor, so **every interaction
is ASK, even with an explicit grant.** That is the intended Phase 4 posture.

A consequence worth stating: because the floor fires before the taint rule,
`taint_escalation` does not appear in an interaction's applied rules. The
outcome is identical (ASK) but an audit reader will not see taint recorded as
the cause. It is masked, not absent —
`test_taint_escalation_reaches_browser_resources` pins the mechanism separately
so relaxing the floor later cannot silently remove it.

### A documented sharp edge

Grants match with `fnmatch`, where `*` crosses dots. A scope of
`browser:https://github.com*` therefore also matches
`browser:https://github.com.evil.com:443`. That is the existing engine's
behaviour and Step 4 does not change it. Exact scopes are safe, and a test pins
the exact-scope case; **UI- and seed-generated scopes should be exact.**

---

## 4. Element references (4D)

`jarvis/browser/elements.py`. Page-scoped, generation-stamped, issued only by
the registry in response to an inspection that actually found the element.

Not coordinates: `click(840, 312)` clicks whatever is there when the click
lands, reports success, and cannot be described in a confirmation dialog.

Not raw selectors either: a selector is a *query*, re-run at action time
against whatever the page has become. `#submit` still resolves after a
navigation — to a different button. And a selector is a string the model can
compose, so it can act on an element nobody inspected.

A reference dies when its page closes, when its page navigates (generation
bump, wired to Playwright's `framenavigated` so redirects and scripts count
too), and when the browser goes. Validation is by **lookup, not parsing**, so a
plausible-looking invented id resolves to nothing, and rewriting the page id on
a real reference does not make it work elsewhere.

Bounded: entries hold locators, locators hold pages. Per-page cap with
oldest-first eviction, cleared on navigation, page close, and shutdown.

---

## 5. Credential refusal (4E)

JARVIS does not type passwords — not "is instructed not to", but *cannot*,
because the check runs against the live DOM before any text is entered.

A page is untrusted content and can argue with a prompt instruction. It cannot
argue with a function that reads `input[type="password"]` and returns a
refusal.

Identification is two-tier, and the second tier exists because of a bug this
found in its own first draft: `pin` as a substring fires on **shipping**. A
rule that refuses the shipping address is a rule the user turns off, taking the
password check with it. So long unambiguous markers (`password`, `apikey`,
`seedphrase`, …) match as substrings with separators removed — one entry covers
`API key`, `api-key`, `api_key`, `apiKey` — and short ambiguous ones (`pin`,
`otp`, `cvv`, `token`, …) match on word boundaries after a camelCase split.

Fails closed: an element whose DOM payload could not be read is treated as a
password field. The only reason it would be unreadable is that JARVIS cannot
see it, and an element JARVIS cannot see is not one to type a secret into.

There is no credential store, no keyring integration, no access to saved
browser passwords. Refusal is the whole feature; its absence of a counterpart
is what makes it a property of the build rather than a policy that could be
relaxed.

---

## What is deliberately not done

- **No browser tools.** A test asserts the registry contains none.
- **No navigation.** A test asserts nothing in the package calls `page.goto`,
  `set_content` or `route` — the URL policy only protects what goes through it,
  and a tool reaching for `goto` directly would skip it entirely while looking
  correct.
- **No confirmation plumbing for browser actions.** `BrowserPolicy` returns a
  decision; turning ASK into a persisted confirmation is Step 5's job, and it
  must use the existing `ConfirmationService` rather than inventing one.

## Residual gaps

- **DNS rebinding is not defeated.** The policy resolves at check time; the
  browser resolves again when it connects. Closing this needs connection-level
  control Playwright does not expose. Stated rather than implied to be covered.
- **`fnmatch` wildcards cross dots** — see the sharp edge above.
- **Nothing enforces the ordering** URL-check → origin → permission → action.
  It cannot be enforced until there is an action; Step 5 must wire it, and the
  no-navigation test is the guard rail that makes skipping it visible.
