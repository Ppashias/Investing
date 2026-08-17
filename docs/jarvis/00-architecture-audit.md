# JARVIS — Phase 0 Architecture Audit

**Repository:** `Ppashias/Investing`
**Audited:** 10 August 2026
**Commit at audit:** `af5d35d` ("v26: RATE OF RETURN follows the selected graph period")
**Status:** Assessment only. No production code was written or changed. Awaiting approval before Phase 1.

---

## 0. Executive summary

The headline finding is that **there is no existing JARVIS to extend.** The repository contains exactly one file — `index.html`, 1,526 lines — and that file has been the only file in the repository's entire 26-commit history. It is a self-contained, dependency-free, single-page Trading 212 portfolio dashboard called "Portfolio Nebula." There is no backend in this repository, no database, no authentication, no AI functionality of any kind, no build system, no tests, and no package manifest.

This is not a criticism of the existing work. Judged as what it actually is — a personal, mobile-first, install-to-home-screen financial dashboard with a genuinely impressive procedural canvas visualisation — it is competent and cohesive. But it shares approximately nothing with the architecture JARVIS requires. The overlap is one idea (described in §10) and one visual component.

The most consequential structural fact is that **the backend that actually matters is not in this repository.** The dashboard talks to a Cloudflare Worker that holds the Trading 212 API key, and that Worker's source code has never been committed anywhere I can see. The system's only privileged component is therefore unversioned, unreviewed, and unrecoverable if the Cloudflare account is lost.

**The most urgent finding has nothing to do with JARVIS and should be acted on today.** This repository is public, with GitHub Pages enabled, and line 310 of `index.html` embeds roughly 380 days of your actual account values — a complete personal net-worth history — alongside your holdings and position sizes. It is readable by anyone right now, both on github.com and on the served page. Making the repository private takes seconds and is the single largest risk reduction available; the full remediation sequence is in §13.5.

The second security finding is architectural: **the shared access token is transmitted in URL query strings and deliberately retained in the browser address bar**, on a page that also injects a third-party script from `s3.tradingview.com` into the same origin, with no Content-Security-Policy. Details in §13.1–§13.3.

My recommendation is to treat the existing dashboard as a **feature module** that JARVIS will eventually absorb — specifically, as the seed of the Finance module's data layer — and to build the JARVIS core as a new, separate, local-first system alongside it. Do not attempt to grow the core out of `index.html`. Do not rewrite the dashboard either; it works, and rewriting it delivers no capability.

I also disagree with two points in the brief as written, and I want to say so before you approve a roadmap built on them. They are the permission model (§14.4) and the meaning of "provider-agnostic" (§14.6). Both are fixable with small changes to the design, but they are load-bearing, so they are worth resolving now rather than in Phase 6.

One piece of unexpectedly good news: **Unreal Engine 5.8 ships a first-party MCP plugin**, which makes the deep Unreal integration in Phase 9 substantially less speculative than the brief assumes — and, together with MCP's move to Linux Foundation governance, is the main reason I am comfortable making MCP the load-bearing extension mechanism for the whole system (§14.3, §18).

---

## 1. Current architecture

There is one architectural layer. The entire application is a single HTML document with an inline `<style>` block and a single inline `<script>` wrapped in an IIFE under `"use strict"`.

```
Browser (iOS home-screen app / desktop)
│
└── index.html  ── 1,526 lines, zero dependencies at rest
    ├── <style>       lines  12–86     dark theme, CSS custom properties
    ├── <body> markup lines  88–179    header, hero card, canvas, table, chart modal
    └── <script>      lines 181–1524   one IIFE containing everything:
        ├── data model + localStorage persistence      185–218
        ├── formatting + P/L colour scoring            220–232
        ├── procedural planet-texture generator        234–295
        ├── account-value hero, history, sparkline     297–554
        ├── holdings table + edit/backup UI            555–620
        ├── nebula simulation (layout + physics)       622–818
        ├── nebula renderer (~380 lines of canvas)     820–1202
        ├── tooltip, hit-testing, drag physics         1204–1334
        ├── TradingView modal integration              1249–1290
        ├── live data client (T212 relay + CoinGecko)  1342–1496
        └── self-updater + polling timers              1498–1522
```

Runtime data flow:

```
Trading 212 API ──▶ Cloudflare Worker ──▶ index.html ──▶ localStorage
  (holds secret)     (holds T212 key,      (fetch, 30s)   (8 keys)
                      exposes 3 endpoints)
                                          ◀── CoinGecko (BTC/EUR, 60s)
                                          ◀── s3.tradingview.com (chart widget)
```

Three Worker endpoints are exercised by the client: `/portfolio` (line 1443), `/transactions` (line 369), and `/prices` (line 1469). Their implementation lives outside this repository.

---

## 2. Current technology stack

| Layer | What is actually there |
|---|---|
| Language | ES5-flavoured JavaScript (`var`, `function`, no classes, no modules) |
| Framework | None |
| Build | None — no bundler, no transpiler, no minifier |
| Package manager | None — no `package.json`, no lockfile |
| Styling | Hand-written CSS with custom properties, dark-only (`color-scheme: dark`) |
| Rendering | Canvas 2D, `requestAnimationFrame` loop, offscreen texture caching |
| Persistence | `localStorage`, 8 keys, JSON-serialised |
| Networking | `fetch`, polling on `setInterval` |
| Backend | Cloudflare Worker (**source not in this repository**) |
| Hosting | Static file; the self-updater at line 1502 re-fetches `location.pathname`, implying plain static hosting |
| Tests | None |
| CI | None |
| Linting / formatting | None |
| Types | None |

Two runtime third-party dependencies are loaded from the network at use time, neither pinned nor integrity-checked:

- `https://s3.tradingview.com/tv.js`, injected as a `<script>` element on first chart open (lines 1254–1258)
- `https://api.coingecko.com/api/v3/simple/price`, called every 60 seconds (line 1459)

---

## 3. Current frontend

The frontend *is* the application. Notable properties, both good and bad:

**Genuinely good.** The canvas engine is the most substantial piece of engineering in the file and the part most worth keeping. It renders strategy groups as suns with holdings in elliptical orbit, with per-ticker procedural planet textures seeded by an FNV-1a hash of the symbol (lines 238–295), so each holding has a stable visual identity across sessions. It has gyroscope parallax on iOS with the required `DeviceOrientationEvent.requestPermission` gate (lines 636–659), market-hours-aware sky brightness computed from real `Intl.DateTimeFormat` timezone data for Xetra and NYSE (lines 662–673), comet tails scaled to intraday move, and pointer-drag physics with spring-back. The DPR handling is correct and capped at 2. This is careful work.

**The financial logic is also non-trivial and correct-looking.** Period P/L is deposit- and withdrawal-adjusted by paginating the relay's transaction history and subtracting external cash flows from the period delta (lines 344–444), which is the right way to compute performance rather than balance change. The pie-cash and Spending-pot residual reconciliation (lines 1415–1430) exists because T212's headline total includes money the position endpoint does not report — that is domain knowledge someone had to work out, and it should not be thrown away.

**Structurally limiting.** Everything lives in one function scope. There are no modules, so there is no way to add a dependency without either introducing a bundler or loading a CDN script — and CDN scripts are precisely what the security fixes in §13 need to eliminate. Rendering, state, persistence, HTTP, and DOM manipulation are interleaved. There is no component boundary anywhere.

**State is device-local and fragile.** All state is `localStorage` on one device. Clearing site data destroys the recorded history. The code comments at lines 1358–1360 acknowledge that iOS home-screen apps get their own isolated storage, which is why the config token is deliberately kept in the URL — a workaround that trades a real security property for a convenience one (§13.2).

**Honest labelling is already partly present.** The footer distinguishes what is snapshot data from what is live, and the TradingView fallback panel explains exactly why an embed failed rather than showing a broken frame. That instinct matches requirement 26 in your brief and is worth carrying forward as a house rule.

---

## 4. Current backend

**In this repository: none.**

Externally, a Cloudflare Worker acts as a read-only relay. From the client's usage I can infer its contract but not its implementation:

| Endpoint | Query params | Returns (as consumed by the client) |
|---|---|---|
| `GET /portfolio` | `t` | `{positions: [{ticker, quantity, currentPrice, ppl, fxPpl}], cash: {free, blocked, invested, ppl, total, pieCash}}` |
| `GET /transactions` | `t`, `limit`, `cursor`, `time` | `{items: [{type, dateTime, amount}], nextPagePath}` |
| `GET /prices` | `t`, `symbol`, `range` | `{points: [[timestamp, price], …]}` — a Yahoo Finance proxy |

The design intent is sound and is the single most reusable idea in the codebase: **the browser never holds the Trading 212 API key.** The Worker holds it; the browser holds only a shared token scoped to the relay. That is the correct shape for every third-party integration JARVIS will need.

The problems are with the implementation of that idea, not the idea:

1. **The source is not version-controlled.** No history, no review, no rollback, no ability to reason about what the relay actually permits. This is the highest-privilege component in the system and the least visible one.
2. **Authentication is a single shared bearer token passed as a URL query parameter** — see §13.1.
3. **There is no `/orders` or write endpoint in evidence**, and the code comment at line 1344 asserts "Nothing here can trade." I could not verify that claim, because the Worker source is not available. It should be verified and then enforced by the T212 key's own scope, not only by the Worker's routing.

**Action regardless of the JARVIS decision:** commit the Worker source to a repository this week.

---

## 5. Current database

None. There is no database, no schema, no migrations, and no server-side persistence of any kind.

Client-side persistence uses eight `localStorage` keys:

| Key | Line | Contents |
|---|---|---|
| `tft-nebula-portfolio-v2` | 215 | Holdings array — the primary data model |
| `tft-nebula-acctbase-v3` | 301 | Today's opening account value (P/L baseline) |
| `tft-nebula-hist-v3` | 301 | Per-minute account value, 25h rolling, capped at 600 points |
| `tft-nebula-daily-v1` | 301 | One point per calendar day, capped at 750 |
| `tft-nebula-range-v1` | 301 | Selected chart range (`1D`…`YTD`) |
| `tft-nebula-flows-v1` | 348 | Deposit/withdrawal history + pagination cursor |
| `tft-nebula-base-v1` | 1378 | Per-holding daily opening values (comet-tail baselines) |
| `tft-nebula-livecfg-v1` | 1345 | **Worker URL, access token, BTC quantity** |

The data model itself is a flat array of `{id, name, grp, val, pl}` with a fixed group vocabulary defined at line 185. It is adequate for a dashboard and irrelevant to JARVIS.

One item deserves separate attention. Line 310 contains `SEED_V`, a hard-coded array of roughly 380 daily account-value figures reconstructed from the Trading 212 API and covering the period from account opening in April 2025 onward. **That is a complete personal net-worth time series committed to git, in a repository that is public.** See §13.5 — this is the most urgent finding in the audit and is worth acting on before reading the rest of it.

---

## 6. Existing integrations

| Integration | Direction | Auth | Notes |
|---|---|---|---|
| Trading 212 | Read (via Worker) | T212 API key, held server-side | Correct pattern; Worker unversioned |
| CoinGecko | Read, direct from browser | None (public endpoint) | Rate-limited on mobile networks; has a Worker fallback at line 1469 |
| Yahoo Finance | Read, via Worker `/prices` | Relay token | Used as the CoinGecko fallback |
| TradingView | Embedded widget | None | Third-party script into the app origin — see §13.3 |

That is the complete list. No calendar, no email, no notes, no filesystem, no shell, no OS integration, no notifications.

---

## 7. Existing AI functionality

**None.** There is no AI, no LLM call, no API key for any model provider, no prompt, no embedding, no vector store, no agent, no tool definition, and no chat interface anywhere in the repository.

The word "recommended" appears in the UI (the gold LVMH "recommended dip buy" marker, line 203), but that is a hard-coded constant a human typed. There is no model behind it and it must not be mistaken for one.

For JARVIS, this means Phase 1 starts from zero. That is not a bad position — there is no legacy AI plumbing to unwind, and no premature abstraction to fight.

---

## 8. Existing authentication

**None, in the conventional sense.** There is no user account, no login, no session, no identity, and no multi-user concept.

What exists is a single shared secret — the "access token" — that the browser sends to the Worker on every request. This is a bearer capability, not authentication: anyone holding the string has the same access as you, there is no way to distinguish one holder from another, there is no expiry, and there is no revocation short of changing the token in the Worker and re-provisioning every device.

For a single-user read-only dashboard this is a defensible trade-off. For JARVIS — which will hold OAuth tokens for mail and calendar, have filesystem write access, and eventually be permitted to run shell commands — it is not remotely sufficient. Phase 1 needs a real identity and session model even though there is only ever one user, because the *authorisation* system needs a subject to attach policy to.

---

## 9. Existing file structure

```
Investing/
├── .git/
└── index.html          1,526 lines — the entire project
```

That is the complete tree. `git log --all --diff-filter=A --name-only` confirms `index.html` is the only file ever added across all 26 commits.

There is no `docs/`, `src/`, `tests/`, `.github/`, `README.md`, `.gitignore`, `LICENSE`, or `package.json`. The absence of `.gitignore` is worth fixing immediately regardless of the JARVIS decision, because the first time a `.env` file appears in this directory there is nothing stopping it being committed.

---

## 10. What can be reused

I want to be precise here rather than generous, because "reuse" that forces the new architecture to accommodate old constraints is a cost, not a saving.

**Reuse as a pattern (high value):** the **secret-broker relay**. The browser holds a scoped token; a server-side component holds the real third-party credential and exposes a narrow, purpose-built API. Every JARVIS integration — Gmail, Calendar, Notion, Obsidian sync, any brokerage — should follow exactly this shape. This one idea is worth more than all the code in the file, and it is already validated in production against a real financial API.

**Reuse as a component (high value):** the **nebula canvas engine**, lines 622–1202. It is self-contained, has no dependency on the rest of the file beyond a `data` array and a few colour helpers, and is roughly 580 lines of well-tuned rendering. Porting it into a React/Svelte component is mostly a matter of wrapping the animation loop in a lifecycle hook and passing data in as props. It gives JARVIS a distinctive visual identity on day one, and the same engine generalises beyond finance — the sun/orbit metaphor maps naturally onto "projects and their tasks" or "agents and their tool calls."

**Reuse as domain logic (medium value):** the **flow-adjusted return calculation** (lines 344–444) and the **T212 cash reconciliation** (lines 1415–1430). These encode real, hard-won understanding of how Trading 212 reports balances. Whoever writes the Finance module should read these functions before writing anything, even if the final code looks different.

**Reuse as a small UX idea (low value, near-zero cost):** the **build-marker self-updater** (lines 1498–1517). A page that notices it is stale and offers a one-tap refresh is a nice touch for a long-lived dashboard.

**Do not reuse:** the storage layer, the data model, the config UI, the token transport, the global-scope structure, or the polling architecture. All of these are correct for a one-file dashboard and wrong for a platform.

---

## 11. What should be changed

Ordered by how much they constrain what comes next.

1. **Version-control the Cloudflare Worker.** Highest priority and independent of everything else. The privileged component must be in git.
2. **Stop putting the token in URLs** (§13.1) and stop retaining it in the address bar (§13.2). Move to an `Authorization` header and a config mechanism that does not persist the secret in browser history.
3. **Add a Content-Security-Policy and remove the runtime CDN script injection** (§13.3). These two go together: a CSP that would actually protect the token is incompatible with loading `tv.js` from a third party into the same origin.
4. **Introduce a build system and a module boundary.** Nothing in the security or capability roadmap is achievable while the application is one global scope with no way to add a dependency.
5. **Move state off `localStorage` and onto a real datastore.** Device-local, silently-lossy state is unacceptable for memory, tasks, and audit logs.
6. **Introduce a schema with migrations** before any persistent JARVIS data exists. Retrofitting migrations onto an ad-hoc store is far more expensive than starting with them.
7. **Add a test harness and CI.** There is currently no automated way to know whether a change broke anything. Once an agent is writing code into this repository, that stops being a mild inconvenience and becomes a genuine hazard.
8. **Escape or eliminate `innerHTML` on remote-derived data** (§13.4).

Explicitly *not* on this list: rewriting the dashboard's visual design, restructuring the canvas engine, or changing the financial calculations. Those work.

---

## 12. What is missing

Everything JARVIS needs. Rather than restate the brief, here is the gap expressed as concrete components that do not exist and will have to be built:

**Core** — orchestrator, intent classifier, planner, task-state machine, context assembler, failure/retry handling, confirmation broker, progress reporter, post-task review.

**AI layer** — provider abstraction, model router, prompt/context management, streaming, token accounting, cost tracking, caching strategy.

**Memory** — short-term, working, long-term, episodic, semantic, and project-scoped stores; embedding pipeline; semantic search; edit/delete/explain surfaces; permission-aware retrieval.

**Tools** — a standardised tool interface, a registry, schema validation, execution sandboxing, per-tool policy, result normalisation, and an audit trail.

**Agents** — an agent framework, agent registry, delegation protocol, inter-agent messaging, and the nine specialised agents named in the brief.

**Computer control** — screen capture, screen understanding, input synthesis, window management, application detection, process control, a scoped filesystem API, and a gated command executor.

**Browser control** — a managed browser instance, navigation, extraction, form interaction, download/upload handling, and prompt-injection defence for fetched content.

**Personal management** — calendar, tasks, projects, documents, the daily planner, and the life-domain modules.

**Platform** — identity/session, permission engine, secrets management, audit log, background job runner, scheduler, webhook receiver, observability, and backup/restore.

**Nothing in this list exists today. All of it is NOT IMPLEMENTED.**

---

## 13. Security concerns

Findings in the current system, in severity order. Each is real and reproducible from the source, not hypothetical.

### 13.1 — HIGH: access token transmitted in URL query strings

`index.html:369`, `:1443`, `:1469`

```js
var u = normUrl(LIVECFG.url)+"/portfolio?t="+encodeURIComponent(LIVECFG.token);
```

Every request puts the shared secret in the request line. Consequences: Cloudflare's request logs record it by default; it lands in browser history; it is exposed in the `Referer` header of any outbound navigation from the page; and it appears in any proxy or observability tooling between the browser and the edge.

**Fix:** send it as `Authorization: Bearer <token>`. This requires a corresponding change in the Worker, which is another reason the Worker needs to be in git first.

### 13.2 — HIGH: token deliberately persisted in the address bar

`index.html:1352–1363`

```js
if(q.get("wu")&&q.get("t")){
  LIVECFG.url=normUrl(q.get("wu")); LIVECFG.token=q.get("t").trim();
  cfgSave(LIVECFG);
  /* deliberately KEEP the params in the URL: "Add to Home Screen" copies the
     current URL, and iOS home-screen apps get their own empty storage — the
     params re-configure the app on every launch, so the icon always goes live */
}
```

This is a considered decision with a real problem behind it, and I want to acknowledge that before disagreeing with it. iOS home-screen web apps genuinely do get isolated storage, and re-provisioning on every launch genuinely does solve that.

But the cost is that the secret is now permanently in the URL: in browser history, in any bookmark, in any screenshot of the address bar, in any shared link, and in the `Referer` of outbound navigations. The page opens TradingView links via `target="_blank"` (line 170), which is exactly such a navigation.

**Fix:** keep the one-tap provisioning flow but make it single-use. Have the Worker issue a short-lived provisioning code that the client exchanges once for a device-scoped token stored in IndexedDB, then `history.replaceState` the code out of the URL. The home-screen app re-provisions from a stored device token rather than from a URL-borne secret.

### 13.3 — HIGH: third-party script injected into the token's origin, with no CSP and no SRI

`index.html:1254–1258`

```js
var s=document.createElement("script");
s.src="https://s3.tradingview.com/tv.js";
```

`tv.js` is unpinned, has no `integrity` attribute, and executes with full access to the origin — including `localStorage`, which holds the Worker URL and token (line 1345). If `s3.tradingview.com` is ever compromised or DNS-hijacked, the attacker reads the relay credential directly. There is no CSP anywhere in the document to constrain this.

**Fix (two options, both acceptable):** move the chart into a sandboxed `<iframe>` on a separate origin with no access to app storage, or drop the embed and keep only the existing external "tradingview ↗" link. Then add a strict CSP. For JARVIS this becomes a hard rule: **no third-party script executes in the origin that holds credentials.**

### 13.4 — MEDIUM: remote-controlled data reaches `innerHTML`

`index.html:591–592` and `:1216–1229`

```js
tr.innerHTML='<td><strong>'+p.id+'</strong>…<span class="tag">'+p.name+'</span>…';
```

`p.name` is populated from the live feed at line 1413 (`name: pos.ticker`) — i.e. from data the Worker returns, which originates at Trading 212. The tooltip path has the same shape. Today the practical exploitability is low because tickers are well-formed. But this is an injection path from remote data into the DOM with no escaping, and the exposure grows the moment any other data source is added.

**Fix:** use `textContent` for all data-derived values, or a templating layer that escapes by default.

### 13.5 — CRITICAL (confirmed): personal financial history is published publicly

`index.html:310`

I checked, and this is not hypothetical. The GitHub API reports `Ppashias/Investing` as **`"visibility": "public"`, `"private": false`**, with **GitHub Pages enabled** (`"has_pages": true`).

The `SEED_V` array at line 310 is approximately 380 consecutive daily account-value figures — a complete personal net-worth time series from account opening in April 2025 through the present, accurate to the cent. It is currently readable by anyone, in two places at once: in the repository source on github.com, and in the served page on the Pages site.

The surrounding code makes it fully interpretable rather than an anonymous list of numbers. `SEED_T0` (line 309) pins the series to a start date, `SEED_STEP` gives the interval, and the comment at lines 306–308 states plainly that it was reconstructed from Trading 212 orders and transactions since account opening. The `DEFAULT` array at line 189 discloses the actual holdings and position sizes alongside it.

**Remediation, in order:**

1. **Make the repository private now.** This is the single fastest risk reduction and takes seconds. If the Pages site is needed, publish the built page from a separate deployment rather than from a public source repository.
2. **Move `SEED_V` out of the source entirely.** It should be fetched from the Worker at runtime (authenticated), not embedded in a static asset. Note that flipping the repository to private does *not* fix the Pages exposure if Pages continues to serve the same file — the seed data has to leave the shipped artifact too.
3. **Treat the history as already disclosed.** Making the repository private removes future access but not past access; the data has been publicly reachable and may be in caches, forks, or archives. Deleting the array from `HEAD` also does not remove it from git objects — that requires history rewriting, and it is only worth doing in combination with steps 1 and 2.
4. **Rotate the Trading 212 API key and the relay token** as a precaution. No credential appears in the committed source (I checked — the token lives only in `localStorage` and the URL), so this is precautionary rather than a confirmed key leak. But given §13.1 and §13.2 put the token in URLs and browser history, rotation is cheap insurance.

**A related leak worth noting while you are in here.** Because the token is retained in the address bar (§13.2) and the page contains an external `target="_blank"` link to tradingview.com (line 170), clicking that link sends the current URL — token included — to TradingView in the `Referer` header. Adding `rel="noreferrer"` (the link currently has only `noopener`) mitigates that specific path immediately, though the real fix is getting the token out of the URL.

### 13.6 — LOW: no `.gitignore`

Nothing prevents a future `.env`, key file, or credential dump from being committed. Trivial to fix, and it should be fixed before any JARVIS work adds configuration files.

### 13.7 — Forward-looking: the risks JARVIS adds that do not exist today

The current app cannot damage anything — it is read-only, sandboxed in a browser, and touches no local resource. JARVIS deliberately dismantles every one of those protections. The threat model changes categorically:

- **Prompt injection becomes a code-execution path.** A malicious web page read by the Research Agent, or a hostile string in an email, can attempt to steer an agent that holds shell and filesystem access. This is the single most important security property of the whole system, and it is why §14.4 argues for treating untrusted content as a first-class taint rather than a prompt-engineering problem.
- **Credential blast radius grows enormously.** One compromised process would hold mail, calendar, brokerage, and cloud credentials simultaneously.
- **Destructive actions become possible.** File deletion, `git push --force`, sending messages, spending money.
- **The audit trail becomes a security control**, not a debugging convenience — it is how you find out what an autonomous run actually did.

---

## 14. Recommended architecture

### 14.1 The organising decision: local-first, with a cloud brain

Everything in the brief that is distinctive — controlling the PC, driving Unreal Engine, opening Blender, organising local files, running builds — is inherently local. Everything that needs a frontier model is inherently remote. So the architecture is fixed by the requirements, not by preference: **a local daemon owns all capability and all state; remote model APIs are called as a service, over an abstraction, and are never in the trust path for authorisation.**

Concretely, four processes:

```
┌──────────────────────────────────────────────────────────────────┐
│                    YOUR PC (the trust boundary)                  │
│                                                                  │
│  ┌────────────────┐        ┌──────────────────────────────────┐  │
│  │  jarvis-web    │◀──────▶│         jarvis-core              │  │
│  │  (command      │  HTTP  │                                  │  │
│  │   center UI)   │  + SSE │  Orchestrator · Planner          │  │
│  └────────────────┘        │  Memory · Tasks · Projects       │  │
│                            │  Tool Registry · Agent Registry  │  │
│                            │  ┌────────────────────────────┐  │  │
│                            │  │  PERMISSION BROKER         │  │  │
│                            │  │  every tool call passes    │  │  │
│                            │  │  through here. no bypass.  │  │  │
│                            │  └────────────┬───────────────┘  │  │
│                            │               │                  │  │
│                            │  ┌────────────▼───────────────┐  │  │
│                            │  │  AUDIT LOG (append-only)   │  │  │
│                            │  └────────────────────────────┘  │  │
│                            └───────┬──────────────┬───────────┘  │
│                                    │ MCP          │              │
│                    ┌───────────────▼───┐   ┌──────▼───────────┐  │
│                    │   jarvis-host     │   │  MCP tool        │  │
│                    │   (PRIVILEGED)    │   │  servers         │  │
│                    │  screen · input   │   │  browser · files │  │
│                    │  apps · shell     │   │  notes · unreal  │  │
│                    └───────────────────┘   └──────────────────┘  │
│                                                                  │
│                    ┌───────────────────┐                         │
│                    │  SQLite + vectors │  ← all state lives here │
│                    └───────────────────┘                         │
└──────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼  outbound only, no inbound
                       ┌────────────────────────────┐
                       │  Model providers           │
                       │  Anthropic / OpenAI /      │
                       │  Google / local (Ollama)   │
                       └────────────────────────────┘
```

`jarvis-host` is deliberately a **separate process from the core**, running with the OS privileges that screen capture, input synthesis, and process control require. The core has no such privileges. This means a bug or an injection in the core's LLM-handling code cannot directly synthesise a keystroke — it has to ask the host, and the host checks with the permission broker. That separation is the whole point; it should not be collapsed for convenience later.

### 14.2 Recommended stack

| Component | Recommendation | Why this rather than the alternatives |
|---|---|---|
| Core language | **Python 3.12+** | The computer-control, screen-understanding, and ML-adjacent ecosystems are Python-first. Unreal's own editor scripting is Python. This is the deciding factor. |
| Core framework | **FastAPI** + Uvicorn | Async by default (essential for many concurrent long-running agent tasks), Pydantic schemas double as tool schemas and API contracts, first-class SSE/WebSocket. |
| Datastore (relational) | **SQLite (WAL mode)** | Local-first, zero services, one file to back up, and completely boring in the way infrastructure should be. |
| Datastore (vectors) | **LanceDB** | Genuinely embedded and in-process like SQLite, but with disk-based indexing so the semantic store can outgrow RAM. See the note below on why not `sqlite-vec`. |
| Migrations | **Alembic** | Non-negotiable per requirement 22. Schema-first from commit one. |
| Tool protocol | **MCP (Model Context Protocol)** | This is the key architectural bet — see §14.3. |
| Agent harness (coding/computer) | **Claude Agent SDK** (`claude-agent-sdk`) | Claude Code packaged as a library: the agent loop, context management, built-in file/shell/search tools, subagents, hooks, and permission callbacks already exist and are battle-tested. Building this from scratch is months of work for a worse result. |
| Direct model calls | **Anthropic Python SDK** behind a provider interface | For planning, classification, memory summarisation — anything that is one call rather than an agent loop. |
| Frontend | **React + Vite + TypeScript** | Nothing in the existing frontend constrains this choice (one file, no framework). React has the deepest ecosystem for the dashboard/graph/terminal widgets JARVIS needs, and the largest training corpus, which matters when an agent is writing the UI. |
| Frontend state | **TanStack Query** + Zustand | Server state and UI state are different problems; treating them the same is the usual cause of dashboard state bugs. |
| Background jobs | **APScheduler 3.11.x** (Phase 1–7) → **arq** + Redis (Phase 10) | Do not introduce Celery or a broker on day one for a single-user system — a desktop agent that stops working because Redis isn't running is a reliability regression, not an architecture. Move only when autonomous jobs genuinely need durability across restarts. **Pin to 3.11.x and do not write against the 4.0 API:** 4.0 has been in alpha since 2023 with its most recent alpha over a year old, while the 3.x line continues to ship. Treat 4.0 as indefinitely deferred. |
| Secrets | **OS keychain** via `keyring` (Windows Credential Manager, DPAPI-bound to your user account) | Never in the database, never in `.env` at rest, never sent to the frontend. |
| Testing | **pytest** + **Playwright** | Playwright is needed for the Browser Agent anyway, so the same dependency covers E2E tests. |

**A note on the two-store split, since one store is obviously simpler.** My first instinct was SQLite plus the `sqlite-vec` extension, which would keep relational rows and embeddings in a single file and a single transaction. I checked, and I no longer recommend it: `sqlite-vec` is still pre-1.0 after roughly two years, its release cadence has slowed to an alpha, and there is an open question on its tracker about whether it is still maintained. That is an acceptable risk for a weekend project and not one I would put a memory system on.

So: SQLite is the source of truth, and LanceDB is a **derived index**. Memory rows, their text, and their metadata live in SQLite; embeddings live in LanceDB keyed by memory ID. Nothing is authoritative in LanceDB, which means a divergence is repaired by re-embedding from SQLite rather than by reconciliation, and a failed vector write degrades retrieval rather than losing data. It also keeps the "forget that" path honest: delete the SQLite row, delete the vector, write the tombstone, and if the vector delete fails the next reindex removes it anyway.

The alternative worth knowing about is Postgres with `pgvector` (0.8.6+), which is the stronger choice the moment Postgres is in the stack for any other reason — it is a mature extension, not a service, and it puts rows and vectors back in one transaction. I am not recommending it now only because nothing else in the Phase 1–7 design needs Postgres, and adding a database service to a desktop agent is a real operational cost for a single user. If cloud sync or multi-device arrives (§20), migrating to Postgres + pgvector is the expected path, and keeping SQLite's schema conservative is what keeps that a port rather than a rewrite.

### 14.3 Why MCP is the load-bearing choice

Requirement 28 asks that new capabilities be addable as `AGENT + TOOL + PERMISSION + MEMORY + UI` without rewriting the core. MCP is the mechanism that makes that literally true rather than aspirational.

If every capability is an MCP server — the filesystem tools, the browser, Obsidian, Notion, the Unreal bridge, the finance relay — then the core never imports capability code. It discovers tool schemas at runtime from a manifest, applies policy by tool name and arguments, and executes over a transport. Adding Blender support becomes "write a Blender MCP server and register it," with zero core changes. Removing a capability is deleting a config entry.

It also means capabilities are independently testable, independently crashable, and independently permissioned, and that a capability written in another language (a C# Unreal helper, a Node browser driver) drops in with no FFI.

The concrete rule I would adopt: **the core contains no integration code.** If it talks to something outside the process, it goes behind MCP.

**The governance risk on this bet is lower than it was.** MCP is no longer an Anthropic-controlled protocol — it was donated to the Linux Foundation's Agentic AI Foundation in December 2025, whose platinum members include AWS, Anthropic, Block, Bloomberg, Cloudflare, Google, Microsoft, and OpenAI. Betting the extension model on a vendor-neutral standard with that backing is a materially different proposition from betting it on one vendor's protocol, and it is the main reason I am comfortable making MCP load-bearing rather than an optional adapter.

Two implementation constraints from the current spec revision (`2026-07-28`), which matter because getting them wrong means rework:

- **Use Streamable HTTP or stdio, never HTTP+SSE.** The old two-endpoint SSE transport is formally deprecated with a removal timeline. Local servers should use stdio; anything networked uses Streamable HTTP.
- **Do not design around Roots, Sampling, or Logging.** All three were deprecated in the `2026-07-28` revision. In practice: pass paths as explicit tool parameters rather than via Roots; call model providers directly through our own provider abstraction rather than via Sampling (which we want anyway, so the router stays in control); and use structured logging plus the audit log rather than the MCP logging capability. Designing around Sampling in particular would have put model selection inside the tool servers, which is exactly where we do not want it.

### 14.4 Where I disagree with the brief: permission levels

The brief describes permissions as a ladder, Level 0 through Level 7, implying that each level subsumes the ones below it. I think that model will cause real problems and I would like to change it before it is implemented.

The issue is that the levels are not actually ordered. Level 3 (browser) is not a superset of Level 2 (local files) — they are unrelated capabilities with different risks. Granting terminal access (5) does not imply you want autonomous workflows (6). And a single scalar cannot express the thing you will actually want, which is "read anything under `D:\Projects`, write only under `D:\Projects\Unreal`, never touch `C:\Users\...\Documents`."

**What I recommend instead:** a capability matrix, with the eight levels kept as *UI presets* that select a bundle of capabilities. The underlying model is:

```
grant := (capability, resource_scope, mode, conditions)

capability     screen.read | input.write | fs.read | fs.write | app.control |
               shell.exec | net.browse | integration.<name> | …
resource_scope path globs, application allow-list, domain allow-list, …
mode           allow | ask | deny
conditions     autonomy_mode, time_window, per-session budget, reversibility
```

Every tool call is evaluated as a `(capability, resource)` pair against this matrix, and the answer is one of three values, not two. The "ask" state is what makes supervised and semi-autonomous modes work.

Two additional properties I would build in from the start, because retrofitting them is painful:

**Taint tracking.** Content that came from outside — a fetched web page, an email body, a PDF — is marked as untrusted for the lifetime of the task. Any tool call in a task whose context contains untrusted content is escalated from `allow` to `ask` for every destructive or outward-facing capability. This is a structural defence against prompt injection, and it works even when the prompt-level defences fail. Nothing else in the design protects you as reliably.

**Reversibility as a first-class attribute.** Each tool declares whether its effect is reversible, and how. Reversible operations can be auto-approved at higher autonomy; irreversible ones (delete, send, purchase, force-push) always confirm regardless of level. This maps requirement 18's "reversible operations" onto something the policy engine can actually evaluate.

I would also move the permission engine and audit log from Phase 11 to **Phase 1**. Building computer control (Phase 4) on top of a permission system that does not exist yet means either shipping unguarded computer control or rewriting Phase 4 later. Neither is acceptable.

### 14.5 Memory design

Six stores as the brief specifies, but implemented as one table with a discriminator plus scoped indices rather than six subsystems — the retrieval, permission, and editing logic is shared, and duplicating it six times is how memory systems become unmaintainable.

| Store | Lifetime | Retrieval | Notes |
|---|---|---|---|
| Short-term | Current conversation | Recency | Kept in the context window, compacted on overflow |
| Working | Current task | Explicit by task ID | Discarded on task completion, summarised into episodic |
| Long-term | Permanent | Semantic + keyword hybrid | Preferences, decisions, stable facts. Written deliberately, not automatically |
| Episodic | Permanent, decaying relevance | Temporal + semantic | "What was I working on last Tuesday" |
| Semantic | Permanent | Semantic, chunked | Ingested documents and pages, with provenance |
| Project | Permanent, project-scoped | Semantic within project | Isolated per project so context does not bleed |

Every memory row carries: source, created-at, last-accessed, confidence, provenance (which task and which tool produced it), and a sensitivity tag. Provenance is what makes "why do you think that?" answerable, which requirement 6 asks for and which is impossible to add later without it.

"Forget that" must do a real delete of the row *and* its embedding *and* leave a tombstone in the audit log. Soft-delete that still surfaces in retrieval is worse than no delete, because it silently breaks a promise the user thinks was kept.

### 14.6 Where I disagree with the brief: what "provider-agnostic" should mean

The brief asks that JARVIS not be hard-coded to one provider, and that the orchestrator route tasks to appropriate models. I agree with both, and the abstraction should exist from Phase 1. But I want to flag an assumption embedded in the phrasing, because designing around it produces a worse system.

Providers are not interchangeable behind a uniform interface. They differ in ways that matter operationally, not just in quality: which have a computer-use capability at all, how reliable multi-step tool calling is over long horizons, how they behave when a tool errors, context window size, and whether structured output is enforced or merely requested. An abstraction that pretends these are the same forces you to write to the lowest common denominator and throw away the capability you are paying for.

**The design I recommend:** abstract over *task types*, not over *providers*.

```
TaskType declares:  required capabilities (tool_use, vision, computer_use,
                    long_context, structured_output), quality tier, latency
                    budget, cost ceiling

Provider declares:  which capabilities it supports, per model

Router:             filters providers to those that satisfy the requirements,
                    then picks by policy (quality / cost / latency / local-only)
```

This satisfies the real requirement — no lock-in, graceful substitution, per-task routing, the ability to force sensitive tasks to a local model — while allowing each provider to be used at its actual capability rather than a shared subset. If a provider cannot do computer use, the router simply never routes computer-use tasks to it, which is honest, instead of the abstraction silently degrading.

**Current default routing** (Anthropic model IDs and list pricing as of this audit; the router makes these configurable, not hard-coded):

| Task type | Default model | Input / output per MTok | Rationale |
|---|---|---|---|
| Orchestration, planning, hard reasoning | `claude-opus-5` | $5 / $25 | 1M context, strongest long-horizon agentic behaviour |
| Coding, computer control | `claude-opus-5` | $5 / $25 | Same; via the Claude Agent SDK harness |
| Routine agent work, summarisation | `claude-sonnet-5` | $3 / $15 | Near-Opus on agentic tasks at lower cost |
| Classification, routing, extraction | `claude-haiku-4-5` | $1 / $5 | High volume, low difficulty |
| Sensitive / offline | Local via Ollama | — | Never leaves the machine |

Image and video generation are a separate provider category with no Anthropic offering; those route to dedicated image/video providers and are covered in Phase 8.

### 14.7 Database schema (initial)

Thirteen tables covering requirement 22, with Alembic migrations from the first commit:

```
users                id, name, created_at, settings_json
projects             id, name, status, description, root_path, created_at, archived_at
tasks                id, project_id, parent_task_id, title, description, status,
                     priority, deadline, agent_id, autonomy_mode, created_at,
                     started_at, completed_at, result_json, error_json
task_dependencies    task_id, depends_on_task_id
agents               id, key, name, kind, system_prompt, default_model,
                     tool_allowlist_json, enabled
conversations        id, project_id, title, created_at, archived_at
messages             id, conversation_id, role, content_json, model, tokens_in,
                     tokens_out, cost_cents, created_at
memories             id, kind, project_id, content, embedding, source, provenance_json,
                     confidence, sensitivity, created_at, last_accessed_at, deleted_at
documents            id, project_id, source_uri, title, mime, hash, ingested_at
document_chunks      id, document_id, ordinal, content, embedding
tool_calls           id, task_id, message_id, tool_name, arguments_json, result_json,
                     status, permission_decision, duration_ms, reversible,
                     undo_token, created_at
permissions          id, capability, resource_scope, mode, conditions_json, granted_at,
                     expires_at, revoked_at
integrations         id, kind, name, status, config_json, credential_ref, last_sync_at
jobs                 id, kind, payload_json, status, run_at, attempts, last_error,
                     created_at
schedules            id, name, cron, timezone, payload_json, enabled, last_run_at,
                     next_run_at
audit_log            id, ts, actor, task_id, event_type, capability, resource,
                     decision, detail_json, prev_hash, hash
```

Two notes. `audit_log` is hash-chained (`prev_hash` → `hash`) so tampering is detectable; it is append-only and never updated. `tool_calls.undo_token` is what makes the reversibility property in §14.4 actionable rather than decorative.

### 14.8 Structured control first, pixels as the fallback

This is a cross-cutting principle rather than a component, and it is worth stating explicitly because the intuitive reading of requirement 4 ("interact with my computer as a human would") points the wrong way.

Screenshot-and-click is the most *general* way to control software and the *worst* way to control any specific application. It is expensive (every step round-trips an image through a vision model), slow, and brittle in a way that fails silently — a theme change or a DPI shift turns a working automation into one that clicks the wrong thing. The industry has converged on the opposite default: Microsoft's own Playwright MCP positions accessibility snapshots as *bypassing* the need for screenshots and visually-tuned models, and the same logic applies to desktop apps via UI Automation.

So the Computer Agent should try control surfaces in this order, and fall through only when the current one cannot express the action:

1. **The application's own scripting interface**, if it has one — Unreal's Python API, Blender's `bpy`, Photoshop's UXP, a CLI. Fastest, most reliable, fully deterministic.
2. **The accessibility tree** — UI Automation on Windows for desktop apps, the DOM/accessibility snapshot for web. Query by name and role, not by coordinate.
3. **Vision and synthesised input** — screenshots to a computer-use-capable model, then mouse and keyboard. Reserved for canvas/WebGL surfaces, custom-drawn UIs, games, and anything with no programmatic surface at all.

The practical consequence for the roadmap is that Phase 4 should build layers 1 and 2 first and treat layer 3 as the escape hatch. Building layer 3 first is tempting because it demos impressively and works everywhere, but it produces an agent that is expensive per action and unreliable in exactly the applications you care most about.

The same ordering applies to which provider handles what. Computer use is the capability where providers differ most sharply — Anthropic's is a client-executed tool, OpenAI's went GA as a `computer` tool on their Responses API driven by general-purpose models, and Google's is a preview `computer_use` tool on their Interactions API with normalised coordinates and built-in prompt-injection screening. None of them host the environment; all of them require you to supply the sandbox and run the action loop. That is precisely the situation the task-type routing in §14.6 is designed for: declare `computer_use` as a required capability, let each provider declare whether it has one, and let the router pick. Do not try to normalise three different action vocabularies behind one interface.

---

## 15. Phased roadmap

The brief's phase ordering is sound with one change I consider important: **permissions, audit, and secrets move from Phase 11 into Phase 1.** Every later phase depends on them, and Phase 4 (computer control) is unsafe without them. The rest of the ordering I would keep.

| Phase | Deliverable | Complexity | Depends on |
|---|---|---|---|
| **0** | **This audit** | ✅ Done | — |
| **1** | Core skeleton: FastAPI, SQLite + Alembic, provider abstraction + router, conversation engine, task engine, **permission broker, audit log, secrets via OS keychain**, minimal chat UI | **L** | 0 |
| **2** | Memory: six stores, embeddings, hybrid search, project scoping, memory management UI, "what do you remember / forget that" | **M** | 1 |
| **3** | Tool system: MCP client, tool registry, schema validation, policy binding, execution + audit wiring, first tools (scoped filesystem read, web search) | **M** | 1 |
| **4** | Computer Agent: **UI Automation tree first**, screenshots/vision as fallback, mouse/keyboard, window + app control, gated shell, live activity view. **Confirm-by-default throughout.** | **XL** | 3 |
| **5** | Browser Agent: managed browser driven by **accessibility snapshots**, navigation, extraction, forms, downloads, research-to-memory pipeline, **injection taint marking** | **L** | 3 |
| **6** | Multi-agent: agent framework, registry, delegation protocol, the specialised agents, orchestration UI | **L** | 3, 4, 5 |
| **7** | Personal management: calendar/mail integrations, projects, daily planner, Today view, life-domain modules | **L** | 2, 3, 6 |
| **8** | Creative: image/video/audio providers, app detection, adapters for installed creative software, asset organisation | **XL** | 4, 6 |
| **9** | Unreal: first-party Unreal MCP plugin as the primary bridge, Python commandlet for headless asset work, UAT for build/cook, C++ authoring, log + error + iterate loop | **L–XL** | 4, 8 |
| **10** | Autonomy: long-running workflows, durable job queue, scheduler, proactive suggestions, autonomy modes, run reports | **L** | all |
| **11** | Hardening: sandboxing, threat modelling, backup/restore, performance, test coverage, observability | **M–L** | all |

**Complexity key:** S ≈ days · M ≈ 1–2 weeks · L ≈ 3–6 weeks · XL ≈ 2–3 months, at a sustained part-time pace with AI assistance. These are honest ranges, not targets. Phases 4, 8, and 9 are XL because each involves integrating with software that was never designed to be automated, where most of the effort is discovering how the target application actually behaves rather than writing code.

**Recommended pace.** Phases 1–3 are the foundation and should be done properly and unhurriedly; everything after depends on their shape. Phase 4 is where the system becomes genuinely powerful and genuinely dangerous — budget extra time for the permission and confirmation work specifically, not just the automation. Phases 8 and 9 can be deferred indefinitely without blocking anything else.

---

## 16. Complexity, risk, and what could go wrong per phase

| Phase | Main technical risk | Mitigation |
|---|---|---|
| 1 | Over-engineering the abstraction before there are two providers to abstract | Build the interface, implement Anthropic only, add the second provider in Phase 6 when the shape is known |
| 2 | Memory that accumulates noise and degrades retrieval | Deliberate writes over automatic capture; confidence + decay; make the memory UI good enough that pruning is easy |
| 3 | Tool schema drift between the registry and the actual servers | Validate schemas at registration; contract tests per MCP server |
| 4 | Brittleness — UI automation breaks whenever an application updates | Prefer accessibility APIs and application-native scripting over pixel matching; treat vision as the fallback, not the primary |
| 5 | Prompt injection from fetched pages | Taint tracking (§14.4); never auto-execute on fetched instructions; strip active content before it reaches the model |
| 6 | Agents thrashing, delegating in loops, or duplicating work | Hard delegation-depth cap; per-task token and wall-clock budgets; explicit "do not delegate what you can do directly" guidance |
| 7 | OAuth token lifecycle and refresh across several providers | One credential broker, keychain-backed, with refresh and revocation handled centrally |
| 8 | Creative applications with no automation surface at all | Detect capability per application; fall back to file-level workflows; label unsupported apps NOT IMPLEMENTED rather than faking it |
| 9 | Blueprint graph logic is not authorable programmatically (confirmed, §18); the first-party MCP plugin is Experimental and executes tools serially on the game thread | Target C++, Python editor scripting, and data assets; queue Unreal tool calls rather than parallelising them; keep the bridge thin so a UE6 port replaces one server |
| 10 | An autonomous run doing something expensive or destructive unattended | Reversibility gating; spend caps; a kill switch; mandatory run reports |
| 11 | Security work deferred until it is too expensive to do | Which is exactly why permissions and audit are in Phase 1, not here |

---

## 17. Dependencies required

**Core (Phase 1–3)** — `fastapi`, `uvicorn[standard]`, `pydantic`, `sqlalchemy`, `alembic`, `lancedb`, `anthropic`, `claude-agent-sdk`, `mcp`, `keyring`, `httpx`, `apscheduler>=3.11,<4`, `structlog`, `pytest`, `pytest-asyncio`.

**Frontend (Phase 1)** — `react`, `react-dom`, `vite`, `typescript`, `@tanstack/react-query`, `zustand`, `tailwindcss`, plus the ported nebula canvas component.

**Computer Agent (Phase 4)** — `pywinauto` with the `uia` backend as the primary control surface, over Microsoft UI Automation. Screen capture and input synthesis as a secondary layer for the cases UIA cannot reach.

A note on what *not* to pick here, because it is the obvious default and it is now the wrong one: **PyAutoGUI is effectively unmaintained** — no release since May 2023, no commits since June 2023, and its `pygetwindow` dependency has been untouched since 2020. It is also architecturally wrong for this job independent of maintenance: it works purely in screen coordinates and pixel matching, with no access to the accessibility tree, so it breaks on DPI changes, theme changes, resolution changes, and window movement. `pywinauto`'s UIA backend queries controls by name, type, and property instead, which is both more robust and far cheaper than round-tripping screenshots through a vision model. `uiautomation` is a viable thinner alternative directly over the UIA COM API, but it is single-maintainer and self-described as spare-time work, so I would treat it as a fallback rather than the default.

Microsoft has also begun shipping a first-party agentic-Windows platform (Execution Containers SDK for process isolation, Entra Agent ID for agent identity, Agent 365 for policy and observability). That is worth re-evaluating at Phase 4 kickoff — it may offer a better-supported isolation boundary than anything we would build — but I have not verified it deeply enough to design around today.

**Browser Agent (Phase 5)** — `playwright` (actively maintained; 1.62.x as of this audit), plus the official `@playwright/mcp` server so the browser is reachable through the same MCP tool path as everything else.

**Later phases** — image/video generation SDKs (Phase 8); for Unreal (Phase 9) the first-party `ModelContextProtocol` + `AllToolsets` editor plugins, with the Python commandlet and UAT driven as subprocesses and Remote Control over plain HTTP/WebSocket only where a running build must be reached; `redis` + `arq` if durable job queuing is needed (Phase 10).

**Local model runtimes (optional, any phase)** — Ollama, `llama.cpp`'s `llama-server`, LM Studio, and vLLM all expose OpenAI-compatible endpoints, so a single OpenAI-shaped adapter in the provider layer covers all four by varying `base_url`. That is a useful property: "support local models" costs one adapter, not four.

**Deliberately not adopted:** LangChain / LlamaIndex / CrewAI. For a single-user system where the agent harness (Claude Agent SDK) and the tool protocol (MCP) are already chosen, these frameworks add an abstraction layer, a dependency-churn surface, and a debugging obstacle without adding capability. Revisit only if a specific need appears that they uniquely solve.

---

## 18. Capabilities requiring external APIs or services

| Capability | Requires | Notes |
|---|---|---|
| All frontier reasoning | Anthropic API key (+ optional OpenAI / Google) | Paid, per-token |
| Web search | A search API | Or scrape via the Browser Agent, with the reliability trade-off that implies |
| Image / video generation | Dedicated generation providers | No Anthropic offering; separate provider category |
| Calendar / mail | Google (or Microsoft) OAuth | Requires an OAuth app registration |
| Notion | Notion integration token | |
| Obsidian | No API — filesystem access to the vault | Markdown files on disk; no service dependency |
| Trading 212 | Existing Cloudflare Worker | Already built; needs the §13 fixes |
| Unreal Engine | No external service | Local editor + local HTTP/WebSocket bridge |
| Text-to-speech / speech-to-text | A speech provider, or a local model | Only if voice interaction is wanted |

### Unreal Engine (requirement 11) — better news than expected, with one hard limit

Since Unreal is called out as a first-class target, I researched the current automation surfaces rather than deferring to Phase 9. The headline is that **Unreal 5.8 ships a first-party MCP plugin**, which means the Phase 9 integration is far less speculative than the roadmap implies.

**The four surfaces, and what each is for:**

| Surface | Status | Use it for |
|---|---|---|
| **Unreal MCP** (plugin id `ModelContextProtocol`) | **Experimental**, first-party, UE 5.8 | The primary integration. Embeds an MCP server in the editor process at `http://127.0.0.1:8000/mcp`. Requires the companion `AllToolsets` plugin — Unreal MCP itself ships no tools; toolsets cover actors, Blueprints, materials, Niagara, Sequencer, assets, levels, meshes, and are extensible in C++ or Python. |
| **Python Editor Script Plugin** | Experimental (in practice, long-used) | Headless content and asset automation. Embedded Python 3.11.8. The commandlet form `UnrealEditor-Cmd.exe Project.uproject -run=pythonscript -script=…` is the one to build on — no UI, fastest. Note it does not auto-load levels, so load one explicitly first. **Editor-only** — not available in PIE, standalone, or cooked builds. |
| **UnrealBuildTool / UAT** | GA | Builds, cooks, packaging. `RunUAT BuildCookRun`. Epic's own docs concede that hand-authoring the arguments is error-prone and not fully documented by `-help`; the sane workflow is to configure a Project Launcher profile, copy the generated command line from the Output Log, and use BuildGraph for multi-step pipelines. |
| **Remote Control API** (HTTP + WebSocket) | Beta | The only surface that works against a **running or packaged** build, not just the editor. Disabled by default outside the editor but enableable with `-RCWebControlEnable`. Reaches anything exposed to Blueprint or Python. |

**Three caveats on the first-party MCP plugin that matter for how we integrate:** it binds to localhost with **no authentication layer** and is explicitly not designed for remote use, which is fine given our architecture but means it must never be exposed beyond the loopback interface. Tool invocations are marshalled onto the game thread and execute **serially**, so the JARVIS side must not issue overlapping calls — the Unreal tool adapter needs a queue, not a thread pool. And it is Experimental, so its API may move.

**The hard limit, confirmed precisely: you cannot author Blueprint graph logic programmatically.** This is worth stating exactly rather than approximately, because the boundary is sharper than "it's difficult." From Python you *can* create a Blueprint asset with a parent class, add member variables with typed pins, add function graphs, edit components via the subobject subsystem, compile the Blueprint, and even expose new nodes from Python via `@unreal.uclass()`/`@unreal.ufunction()`. What you *cannot* do is create and wire individual nodes inside a graph: the API hands back graph handles but exposes no node-spawning methods, and the pin-linking functions are not reflected into Python at all. `.uasset` is binary, so text-diffing or patching is not a workaround.

The practical consequence: **JARVIS can scaffold Blueprint skeletons and write the logic in C++**, exposing Blueprint-callable functions for you to wire up by hand — or a custom C++ plugin can expose node and pin operations, which is precisely why essentially every third-party Unreal MCP server ships one. "JARVIS writes your Blueprints" is not achievable without that plugin work, and I would rather set that expectation now than in Phase 9.

**One strategic note.** UE 5.8 is, per Epic, the last planned major UE5 release, with UE6 in development and UE5 moving to bug-fix maintenance. That does not change the Phase 9 design — these interfaces are unlikely to vanish — but it argues for keeping the Unreal bridge thin and behind MCP, so a UE6 port replaces one tool server rather than touching the core.

**A small practical collision worth noting now:** Unreal MCP binds to port 8000, and so does vLLM by default. If you ever run a local model server alongside the editor, change one of them.

---

## 19. Capabilities that run locally

Everything that touches your machine or your data at rest:

Screen capture and understanding · mouse and keyboard control · application launch, focus, and close · window management · filesystem read and write · shell execution · Unreal Engine control and builds · creative application control · Obsidian vault access · local document indexing and chunking · the database and all memory · the audit log · secrets in the OS keychain · the task engine and scheduler · the web UI (served from localhost) · optionally, local model inference for sensitive or offline work.

The important architectural consequence: **JARVIS remains useful with no internet connection**, degraded to local models and local capability, because state and capability are local. Only reasoning quality depends on the network.

---

## 20. Capabilities requiring cloud infrastructure

Genuinely required: frontier model inference, web search and browsing targets, image and video generation, and any third-party service integration (mail, calendar, brokerage).

**Optional and deliberately not recommended for now:** hosting JARVIS itself in the cloud. It is worth being explicit about why, because it is a tempting default. A cloud-hosted core cannot see your screen, cannot open Unreal, and cannot touch your files — it would need a local agent anyway, at which point you have two systems and a synchronisation problem instead of one system. Remote access to a local JARVIS is better solved with a tunnel or VPN to the local daemon than by moving the daemon.

Cloud infrastructure genuinely earns its place in exactly three cases, none of which apply yet: multi-device sync with conflict resolution, long-running jobs that must survive your PC being off, and inbound webhooks from external services. I would revisit at Phase 10 and not before.

---

## 21. Recommended immediate actions

Independent of whether you approve the JARVIS roadmap, these should happen to the existing system. The first one is the reason I would not wait for a roadmap decision before acting.

1. **Make `Ppashias/Investing` private, today.** It is currently public with Pages enabled, and it publishes ~380 days of your account values plus your holdings and position sizes (§13.5). Flipping visibility takes seconds and is the largest single risk reduction available.
2. **Remove `SEED_V` from the shipped file** and fetch that history from the Worker instead — private visibility alone does not fix it if Pages keeps serving the same artifact.
3. **Commit the Cloudflare Worker source to git.** It is the only privileged component in the system and it currently exists in exactly one place, unversioned.
4. **Move the relay token out of URLs** into an `Authorization` header, and make the one-tap provisioning flow single-use (§13.1, §13.2). As an immediate partial mitigation, add `rel="noreferrer"` to the TradingView link at line 170 so the token stops leaking in the `Referer` header.
5. **Rotate the T212 API key and relay token** once the above are in place.
6. **Add a `.gitignore`** before any configuration files enter the repository.

---

## 22. What I need from you before Phase 1

Four decisions, and then I can produce the Phase 1 design document and start building.

1. **Approve or amend the two disagreements** in §14.4 (permission matrix with taint tracking and reversibility, rather than a linear 0–7 ladder — levels retained as UI presets) and §14.6 (abstract over task types rather than over providers).
2. **Approve moving permissions, audit, and secrets from Phase 11 into Phase 1.** This is the change I feel most strongly about.
3. **Confirm the stack** in §14.2 — in particular Python + FastAPI for the core, SQLite for the datastore, MCP as the tool protocol, and React for the frontend.
4. **Confirm repository strategy.** My recommendation is a new repository for JARVIS, with this one continuing to hold the dashboard until the Finance module absorbs it in Phase 7. Mixing a single-file static dashboard and a multi-process platform in one repository will cause tooling friction from day one.

Nothing in this document has been implemented. Everything described in §12 and §15 is **NOT IMPLEMENTED**, and will be labelled as such in the UI until it is real.

---

## 23. Verification status

Requirement 27 asks for research before implementation rather than assumption, so here is what was actually checked against current sources versus what is my judgement.

**Verified against the repository itself** (read in full, line references throughout): every claim in §1–§13 about the existing code. Repository visibility and Pages status were confirmed via the GitHub API, not inferred.

**Verified against current vendor documentation** during this audit: Anthropic model IDs, pricing, and the Agent SDK / Managed Agents / Tool Runner distinction; the MCP `2026-07-28` spec revision, its transport bindings, its deprecations, and the Linux Foundation governance change; OpenAI's and Google's computer-use surfaces and their current status; Playwright and the official Playwright/Chrome DevTools MCP servers; the maintenance state of PyAutoGUI, pywinauto, and `uiautomation`; pgvector, LanceDB, Chroma, Qdrant, and `sqlite-vec` versions and maintenance; Ollama, llama.cpp, LM Studio, and vLLM OpenAI compatibility; APScheduler, Celery, arq, pg-boss, BullMQ, and Temporal; `keyring`, SOPS, and age; and all four Unreal automation surfaces including the first-party MCP plugin and the precise Blueprint API boundary.

**Explicitly unverified, and flagged as such where it appears:** the Cloudflare Worker's actual implementation and whether it truly cannot trade (§4) — I could not read source that is not in the repository; Microsoft's newer agentic-Windows components beyond the Execution Containers SDK / Entra Agent ID / Agent 365 pillars (§17); and whether OpenAI's GA `computer` tool carries a per-call fee.

**My judgement, not research:** the architecture in §14, the phase ordering and complexity estimates in §15–§16, and the two disagreements in §14.4 and §14.6. Those are arguments, and they are the parts most worth pushing back on.
