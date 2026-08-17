# JARVIS — Phase 1 Security Review

**Scope:** the 68 files added on `claude/jarvis-architecture-audit-3toreo` versus `main` — the whole `jarvis/` tree. Nothing else in the repository is in scope here; the pre-existing dashboard findings are in `00-architecture-audit.md` §13 and are unchanged.
**Reviewed:** 10 August 2026
**Method:** manual review of every changed file, tracing user input to sensitive sinks, plus a sweep for injection sinks (`eval`, `exec`, `pickle`, `yaml.load`, `subprocess`, `os.system`, `innerHTML`, raw SQL string building) across `src/` and `web/`. That sweep returned nothing but comments.

---

## 0. Verdict

No HIGH-severity vulnerability was found in the Phase 1 code. Five findings, one of which I judged worth fixing before declaring the phase complete and did fix (F1). Two more are fixed (F2). The remaining three are documented, deliberate, or deferred with a named prerequisite.

The reason the finding list is short is structural rather than lucky: there is exactly one path to a tool handler, exactly one place a credential is read, and no string-built SQL anywhere. Most classes of vulnerability in the checklist have nowhere to occur.

| # | Finding | Severity | Status |
|---|---|---|---|
| F1 | Approved-but-unconsumed confirmations never went stale | Medium | **Fixed** |
| F2 | Internal error text reached model context and the DB unscrubbed | Low | **Fixed** |
| F3 | Activity endpoints are not user-scoped | Low | Deferred — Phase 2 prerequisite, schema change |
| F4 | `/docs` and `/openapi.json` are unauthenticated outside production | Low | Accepted, documented |
| F5 | Bearer token in `localStorage` | Low | Accepted, documented |

---

## F1 — Approved confirmations never expired: `jarvis/src/jarvis/confirmations/service.py:144`

* **Severity:** Medium
* **Category:** `authorization` / stale-grant
* **Status:** Fixed in this branch.

**Description.** `find_approval()` accepted any `Confirmation` whose status was `APPROVED`, whose fingerprint matched, and which was not yet marked consumed. It applied no age limit. `_expire_if_due()` — the only expiry logic — returns immediately for anything not `PENDING`, so `confirmation_ttl_seconds` bounded only how long a request stayed *answerable*, never how long a decision stayed *usable*. An approval that was granted and then not spent was valid forever.

**Exploit scenario.** The user asks JARVIS to do something that needs approval and approves it. The turn then fails before the tool runs — a provider timeout, a closed tab, a restart. The approval sits in the database, approved and unconsumed, indefinitely. Weeks later the same action is proposed again, with byte-identical arguments; `find_approval` matches it and the tool executes with no prompt. In Phase 1 the blast radius is small (the only `ASK`-able tools are reversible and `LOW` risk). It is not small in Phase 4 or 5, where the proposer of that identical action can be text on a web page rather than the user — a latent approval is precisely what an injected instruction needs, because it converts "ask the human" into "proceed silently".

**Fix applied.** `ConfirmationService` gained `approval_ttl_seconds` (defaulting to the same window as the request TTL, 900s). `find_approval` now skips — and marks `EXPIRED` — any approval older than that window measured from `decided_at`, and treats a missing `decided_at` as unusable. Two tests cover it: `test_stale_approval_stops_authorising` and `test_fresh_approval_is_still_honoured`, the second there so the fix cannot silently break the normal approve-then-resume path.

---

## F2 — Internal error text reached the model and the database unscrubbed: `jarvis/src/jarvis/tools/executor.py:152`

* **Severity:** Low
* **Category:** `data_exposure`
* **Status:** Fixed in this branch.

**Description.** The codebase distinguishes `JarvisError.message` (internal, may quote an exception from deeper in the stack) from `user_message` (safe), and `to_dict()` deliberately omits the former so it never reaches an HTTP response. Two paths bypassed that intent: `execute_safe` fed `exc.message` verbatim into a `tool_result` block — which goes into model context and can be paraphrased back to the caller — and `_finalise_error` persisted `exc.message` to `tool_executions.error_message`.

**Exploit scenario.** A tool wraps a client library that includes a URL or header in its exception text. Today no built-in tool does. Once Phase 3–5 tools wrap HTTP clients, an authenticated-endpoint error string containing a token in a URL would land in the model's context and in the audit table, neither of which passes through the log redactor. This is a discipline failure rather than a live leak: the two-layer redaction in `jarvis/logging.py` was applied to logs only, and these are the two non-log sinks for the same text.

**Fix applied.** Both sites now pass the text through `scrub_text()`, the same function the log pipeline uses, so registered secret values and credential-shaped substrings are removed on every route out of the error object, not just the logging one.

---

## F3 — Activity endpoints are not scoped to a user: `jarvis/src/jarvis/api/routes.py:398` and `:417`

* **Severity:** Low (not currently exploitable)
* **Category:** `authorization`
* **Status:** Deferred, with a named prerequisite.

**Description.** Every other resource route checks ownership — `chat`, `get_conversation`, `archive_conversation`, `get_task`, `update_task`, and `decide_confirmation` all compare a `user_id` and return 404 on mismatch. `/api/activity` and `/api/activity/stream` do not, and the `ActivityBus` broadcasts every event to every subscriber. The root cause is the schema: `activity_logs` has no `user_id` column, so there is nothing to filter on.

**Why it is not exploitable now.** Phase 1 is single-user by construction: `ensure_default_user` returns one operator row and there is no way to create a second. Every activity record belongs to that user already.

**Why it still matters.** It becomes a real cross-tenant read the moment a second principal exists — and Phase 6's specialised agents are exactly that, since a delegated agent subscribing to the bus would see the whole system's activity. Fixing it later is a migration, not an edit.

**Recommendation.** Add `user_id` to `activity_logs` (nullable, backfilled to the default user), filter `ActivityService.recent()` on it, and give `ActivityBus.subscribe()` a user filter. Do this as the first item of Phase 2, before anything else writes to the table.

---

## F4 — Interactive docs are unauthenticated outside production: `jarvis/src/jarvis/api/app.py:51`

* **Severity:** Low
* **Category:** `information_disclosure`
* **Status:** Accepted, documented.

**Description.** `docs_url` is set whenever `environment != "production"`, and the default environment is `development`. FastAPI's `/docs` and `/openapi.json` are not behind `AuthDep`, so any local process can enumerate every endpoint, parameter, and request schema without a token.

**Assessment.** What leaks is the shape of the API, not its data — every endpoint that returns anything still requires the bearer token, and a hostile *web page* cannot read the response either, because the CORS policy does not include arbitrary origins. The disclosure is worth something only to an attacker who is already running code on the machine, and that attacker can read `jarvis/src/` directly. Against that, the docs are the fastest way to explore the system while building it.

**Recommendation if you disagree:** set `JARVIS_ENVIRONMENT=production` in `.env` and the docs disappear. That is already the intended posture for anything long-lived.

---

## F5 — Bearer token stored in `localStorage`: `jarvis/web/assets/app.js:15`

* **Severity:** Low
* **Category:** `data_exposure`
* **Status:** Accepted, documented.

**Description.** The command center persists `JARVIS_API_TOKEN` in `localStorage` so the operator does not retype it. Any script executing in the origin could read it, and it survives browser restarts.

**Assessment.** The mitigating controls are the ones that actually determine whether this is exploitable, and they are all present: `script-src 'self'` with no CDN and no inline script, no third-party code in the origin at all, every DOM insertion via `textContent`, and `frame-ancestors 'none'`. For a script to read the token it would first need script execution in the origin — at which point it can also just call the API directly using the session, so the storage choice stops being the deciding factor. The realistic residual risk is local disk access, which is explicitly out of scope for this review.

**Recommendation.** If you want the stronger posture, switch `TOKEN_KEY` to `sessionStorage` — one word, and the cost is retyping the token once per browser session.

---

## Coverage against the Phase 1 §22 checklist

| Area | Finding |
|---|---|
| **Authentication** | Bearer token on every route except `/api/health`. Constant-time compare via `secrets.compare_digest`. **Fails closed**: auth enabled with no configured token returns 503, never "allow" — tested by `test_auth_fails_closed_when_token_unset`. No session cookies, so no CSRF surface and no session fixation. |
| **Authorization** | Ownership checked on every conversation, task, and confirmation route. Tool authorization goes through the permission engine with no bypass path. F1 and F3 above. |
| **API keys** | No credential is an attribute of `Settings` — only credential *names* are. Resolution happens in `jarvis.secrets` (env → OS keychain). `AnthropicProvider` is the only file that calls `.reveal()`, and it registers the value with the log scrubber as it does. `Secret.__repr__`/`__str__` refuse to print. `test_system_status_never_returns_a_credential` asserts the API surface contains no credential-shaped key. No hard-coded secret anywhere in the diff. |
| **Environment variables** | `.env` is gitignored (`.env`, `.env.*`, `!.env.example`); the repo had no `.gitignore` before this branch. `.env.example` contains placeholders only. |
| **Database access** | SQLAlchemy expression language throughout. Zero raw SQL, zero f-string queries — including the task search, which uses a bound `ilike` parameter (`tasks/service.py:194`). No SQL injection surface. |
| **Frontend/backend separation** | Same-origin, no build step, no CDN, no framework. CSP `default-src 'self'; script-src 'self'`, plus `object-src 'none'`, `base-uri 'none'`, `form-action 'none'`, `frame-ancestors 'none'`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. CORS is loopback-only with `allow_credentials=False`. The token never appears in a URL — the SSE stream uses `fetch` + `ReadableStream` specifically so it can stay in an `Authorization` header. |
| **Tool execution** | Single chokepoint. Schema-validated (Draft 2020-12) → permission decision → timeout-bounded → persisted, every time, including denied and suspended attempts. No handler is reachable another way. Phase 1 ships nothing irreversible and nothing above `LOW` risk, and `test_no_phase1_tool_is_high_risk` fails if that changes. |
| **Injection risks** | No `eval`, `exec`, `pickle`, `yaml.load`, `subprocess`, `os.system`, or shell anywhere. No `innerHTML` — the whole UI builds nodes with `textContent`, so model output and tool results cannot become markup. Prompt injection is addressed structurally rather than by prompt text: the `tainted` flag escalates every non-read capability to `ASK`. Nothing sets it in Phase 1 because Phase 1 ingests no untrusted content; **the obligation is that Phase 3/5 must set it at the ingestion point**, and that is the single most important carry-forward item in this document. |
| **Logging** | Two layers: sensitive key names redacted wholesale, and every resolved secret's literal value scrubbed from rendered strings, with credential-shaped regexes as a backstop for values never registered. Applied as the last processor, so it sees fully-rendered text. F2 extended the same scrubbing to the two non-log sinks. |
| **Permissions** | Capability matrix, most-specific-scope-wins, three-valued. Four fail-safe rules, each with a test that fails if the rule is deleted: defaults deny upward, irreversibility floors `ALLOW`→`ASK`, risk ceilings downgrade only, taint escalates. `SENSITIVE_ACTION` cannot be auto-allowed in Phase 1 by any combination of grants. Seeded grants are minimal — read-only plus two named low-risk tools. |

---

## Carry-forward obligations

These are not Phase 1 defects. They are the conditions under which Phase 1's guarantees stop holding, and each has a phase attached.

1. **Set `tainted` at every untrusted-content ingestion point** (Phase 3 file reads, Phase 5 page fetches, Phase 7 mail bodies). The engine already honours it; if the ingestion side never sets it, the defence is inert and the code will still *look* correct.
2. **Add `user_id` to `activity_logs` before a second principal exists** (F3) — first item of Phase 2, ahead of any other write to that table.
3. **Re-examine the approval TTL when long-running autonomous work lands** (Phase 10). A 15-minute window is right for a human in a chat loop; a job that queues an approval and resumes an hour later will need an explicitly longer, explicitly chosen window rather than a quietly raised default.
4. **The repository is public.** Nothing in `jarvis/` contains a credential, but this document and the Phase 0 audit both describe where sensitive data lives. Audit §13.5 stands: make the repository private.
