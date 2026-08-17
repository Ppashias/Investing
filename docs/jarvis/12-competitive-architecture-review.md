# Competitive architecture review — Phases A–C

**Status: report only. No production code changed.** Phase A said not to, and
Phases D–G are gated on your decisions here.

All three references were cloned and read at source level, not summarised from
READMEs. Commits studied:

| Repository | Commit | Files | Stars | Primary languages |
|---|---|---|---|---|
| `vierisid/jarvis` | `a5103b6` (2026-08-14) | 2,577 | 633 | TypeScript, Go (sidecar), React |
| `dev-core-busy/jarvis` | `585d0c8` (2026-08-17) | 527 | 21 | Python, JS, Kotlin, Go |
| `ONEPUNCHMAN411/Jarvis` | `9bbb3bf` (2026-06-25) | 268 | 0 | Python |

Ours, for scale: 31,534 lines of source across 104 files, 16,906 lines of tests
across 29 files, 1,044 tests.

---

## A. Current JARVIS assessment

### What is already excellent — and better than all three references

**The single chokepoint.** Every tool call goes through `ToolExecutor`:
schema validation → `PermissionEngine.evaluate()` → confirmation → handler →
persisted `ToolExecution` + `ActivityLog`. There is no second path. Two of the
three references have a comparable idea; none enforces it as narrowly.

**Three-valued permissions on `(capability, resource_scope, mode)`.** Most
specific scope wins, DENY breaks ties. `vierisid` uses a numeric 0–10 ladder —
which the Phase 0 audit specifically argued against, and I still think we were
right: browser access is not a superset of file access.

**DENY is unconditional.** This is the sharpest difference found in the whole
review, and it goes our way. `vierisid`'s authority engine checks *temporary
parent grants first*, before per-action overrides:

```ts
// 1. Check temporary grants (parent escalation)
if (grants?.includes(actionCategory)) return { allowed: true, ... };
// 2. Check per-action overrides
if (override && !override.allowed) return { allowed: false, ... };
```

A parent agent's temporary grant therefore beats an explicit `allowed: false`
override. In ours, no grant can override a DENY.

**Structural taint, not pattern matching.** `ToolResult.untrusted` taints the
turn and the permission engine escalates every non-read capability, whether or
not the model was persuaded. `dev-core-busy` detects prompt injection with ~20
regexes plus an LLM classifier — a *detection* layer, which fails differently:
novel phrasing passes. Ours cannot be argued with. Theirs catches things ours
does not classify as content. Both are worth having; only one is structural.

**Irreversibility floors the decision.** No grant makes an irreversible action
silent. None of the three has this.

**The browser control plane** — `UrlPolicy` (scheme + resolved destination +
redirect, now enforced at the Playwright routing layer), element registry
instead of selectors, credential-field refusal. `ONEPUNCHMAN411`'s browser
validates the **scheme only**:

```python
if scheme not in ("http", "https"):
    raise ValueError(...)
```

`http://169.254.169.254/latest/meta-data/` passes that check. Its `click()` also
takes a raw CSS selector, so the model can compose one for an element nobody
inspected. We closed both of those deliberately.

**Test depth.** 1,044 tests, a 13-mutation battery, real Chromium against a real
local server. `vierisid` has meaningful colocated tests (`authority.test.ts` is
652 lines); the other two have very little.

### What is weak

| Weakness | Consequence |
|---|---|
| **No sub-agents.** One agent loop, bounded iterations. | Cannot decompose a goal, cannot run a researcher and a coder concurrently. This is the single largest capability gap. |
| **No background execution.** Zero `asyncio.create_task`, no scheduler, no job table. | A task that outlives one HTTP request cannot exist. No progress, pause, resume, cancel. |
| **No goals.** No goal/milestone/commitment model. | JARVIS is reactive. It cannot follow up, chase, or remind. |
| **No skills/plugins.** Tools are code-declared and compiled in. | Every new capability is a source change and a redeploy. |
| **No voice.** | Not a security gap; a real product gap on Windows. |
| **Model routing is a static map.** `TaskClass → configured model name`. | No cost, latency, privacy or context-size input. |
| **No sandbox below the application.** `FilesystemGuard` and `TerminalExecutor` are Python-level. | A bug in our own guard is the whole boundary. Both Linux references have an OS-level layer underneath. |
| **Memory provenance is thin.** | The brief's "do not allow arbitrary tool output to silently become trusted permanent memory" is not currently guaranteed. |
| **Emergency stop is computer-scoped.** | It does not stop browser or external actions. |
| **No telemetry / self-observation.** | We cannot answer "what is failing most often?" without reading logs by hand. |

---

## B. Best ideas, by repository

### `vierisid/jarvis`

1. **Authority strictly decreases down the hierarchy.**
   ```ts
   max_authority_level: Math.min(role.authority_level,
                                 parent.agent.authority.max_authority_level - 1)
   ```
   A child is always *strictly* below its parent. This is the cleanest answer
   I found to "a sub-agent must never inherit unrestricted permissions", and it
   is one line. **Adopt the principle, not the numeric ladder.**

2. **Impact classification separate from action category.** `read` / `write` /
   `external` / `destructive`, derived from the category by a fixed map. It is
   the axis a human actually reasons about when approving.

3. **The two-tier voice-approval gate.** Destructive impacts *never* resolve by
   voice; non-destructive need STT confidence ≥ 0.85. The reasoning is explicit:
   "a single misheard syllable could trigger a payment." Adopt this *before*
   voice, not after.

4. **`ResolutionChannel` on every audit row** — `click` / `voice` / `system`.
   "Who approved this, and through what?" is answerable from one column. Ours
   records the confirmation but not the channel.

5. **Context rules** — time-of-day and tool-name conditions that produce
   allow/deny/require_approval. "No external actions after 22:00" is currently
   inexpressible for us.

6. **Encrypted keychain with a documented reason.** AES-256-GCM file plus a
   `chmod 600` key, chosen because OS keychain daemons are unreliable on WSL2.
   Directly relevant to you on Windows.

7. **Goal system with accountability and rhythm** (`goals/accountability.ts`,
   `rhythm.ts`) — goals, commitments, deadlines, proactive follow-up.

8. **Deferred executor** — an approved action that runs later, rather than a
   request that dies when the user is away. Ours suspends and resumes on the
   next turn, which is close but requires the user to come back.

**Not adopting:** the 0–10 numeric ladder; temporary-grant-before-deny ordering
(a defect); the Activepieces workflow embed (a large dependency for a capability
we have no demand for yet).

### `dev-core-busy/jarvis`

1. **OS-level egress control.** `shell_execute` for un-privileged users runs as
   a dedicated network-locked OS user (`jarvis_sandbox_noinet`) whose outbound
   traffic is restricted by nftables to loopback + RFC1918 + configured DNS.
   Public internet is dropped *by the kernel*. This is the most valuable idea in
   the entire review: it is a boundary that survives a bug in the application
   layer. Ours has no equivalent — `UrlPolicy` is in-process, and a flaw in it is
   total.

2. **Enforcement placed in dispatch, stated as doctrine.** Their module header:
   *"These checks are ENFORCED in tool dispatch — not in the prompt. They cannot
   be circumvented by prompt, base64 encoding, or 'learned facts'."* Same
   conclusion we reached independently. Useful confirmation.

3. **Symlink resolution before the allowlist check** — explicitly to stop
   `/tmp/link -> /etc/shadow`. Worth verifying ours does the same.

4. **Owner-scoped documents, fail-closed without a registry entry** — added
   after a real incident where `filesystem list` exposed every user's filenames.
   A concrete multi-user lesson available to us for free.

5. **Skill manifests declaring `permissions`, `dependencies`, `system_packages`,
   `data_dirs`, `caches`** — with install/uninstall lifecycle. The right shape
   for our skill system.

6. **A root broker** for privileged operations (apt installs), rather than
   running the daemon as root.

**Not adopting:** account lockout on jailbreak detection. It is a
denial-of-service vector — if a *browsed web page* can get injection-shaped text
in front of the classifier, an attacker locks the victim out of their own
assistant. Their classifier is also deliberately fail-open, so it is
simultaneously bypassable and abusable. Detection yes; automatic lockout no.

**Also not adopting as-is:** skills loaded via `importlib` are arbitrary
in-process Python. A manifest that *declares* permissions does not *enforce*
them when the code runs with full interpreter access.

### `ONEPUNCHMAN411/Jarvis`

1. **The Windows UI Automation accessibility tree** (`control/accessibility.py`,
   pywinauto, `backend="uia"`) — reading structured elements instead of
   screenshots plus vision. This is the desktop analogue of the argument we
   already won in the browser: *named elements, not coordinates*. It is the
   single most useful idea here for your Windows environment, and it makes
   desktop confirmations readable for the same reason element handles made
   browser confirmations readable.

2. **`focus_history.py` / `region_watcher.py`** — knowing what the user was just
   doing, and watching a screen region for change rather than polling
   screenshots.

3. **A dry-run tool** — analyse a shell command and report what it *would* do.
   A genuinely good primitive to offer before a confirmation.

4. **Voice pipeline shape**: VAD → faster-whisper STT → wake word → TTS, with
   voice profiles. Worth copying the *structure* if we do voice.

**Not adopting:** its permission model (a global autonomy mode with
`enforce_policy(action, "confirm")` — no per-resource grants, no taint, no
irreversibility floor); its browser layer, which is materially weaker than ours;
its plugin base class, which declares no capabilities, no permissions, and no
risk — `name`, `enabled`, `get_tools()` and nothing else.

---

## C. Gap analysis, prioritised

### CRITICAL

**C1. No OS-level boundary beneath the application guards.**
*Current:* `FilesystemGuard`, `TerminalExecutor` (no shell, argv, scrubbed env),
`UrlPolicy` — all in-process Python.
*Reference:* dedicated OS user + nftables egress table (`dev-core-busy`).
*Weakness:* every boundary we have is one Python bug away from nothing. Step 11
found exactly such a bug in `UrlPolicy`'s coverage and Step 12 fixed it — but the
class of failure recurs.
*Recommended:* run the terminal executor as a restricted OS user; add a
platform-appropriate egress restriction (nftables on Linux, Windows Firewall
per-app rules on Windows). Keep every existing check — this goes *underneath*.
*Difficulty:* high, platform-specific. *Security impact:* very high.

**C2. Ambient memory capture launders taint. Confirmed at source, not inferred.**

The ingestion paths are correct: `MemorySource.is_external` covers `DOCUMENT`,
`OBSIDIAN` and `WEB`, and `service.py` stores those tainted. The *ambient
capture* path is not.

```python
# orchestrator/stages.py:588 — ctx.tool_taint exists and is NOT passed
).evaluate_exchange(user_id=…, user_message=…, assistant_message=…,
                    conversation_id=…, project_id=…, request_id=…)

# memory/evaluator.py:156 — every ambient candidate, unconditionally
source=MemorySource.CONVERSATION,

# memory/service.py:233
tainted=draft.tainted or draft.source.is_external   # CONVERSATION → False
```

`evaluate_exchange` has no taint parameter at all. So a turn that read a poisoned
page, let it into the assistant's answer, and then had a "fact" extracted from
that exchange produces a memory row marked `CONVERSATION`, **`tainted=False`**,
permanently. `retrieval.py:209` propagates taint from stored rows — so the flag
being wrong at write time means retrieval will never escalate on it either.

*Severity:* **HIGH at the shipped default**, `memory_capture_mode="ask"`, because
candidates land `PROPOSED` and a human says yes or no first. **CRITICAL under
`memory_capture_mode="auto"`**, where they land `ACTIVE` immediately with no
human in the path. Note the mitigation is the *approval*, not the taint flag —
the flag is wrong in both modes, so an approved memory is permanently laundered.

*Recommended:* thread `ctx.tool_taint` into `evaluate_exchange`; mark ambient
candidates from a tainted turn as tainted regardless of source; add provenance
(originating tool + request id) to the row; and consider refusing ambient capture
entirely on a tainted turn under `auto`.
*Difficulty:* low — the plumbing is three call sites. *Security impact:* high.

### HIGH

**C3. No sub-agent model, so no authority-decrease invariant.** Adopt
`Math.min(role, parent - 1)` as a *capability-set* intersection: a child's grants
must be a strict subset of its parent's, and `spawn_agent` must itself be a
gated capability.

**C4. No background execution.** No progress, pause, resume, cancel, retry,
resource limits, or runaway detection. Everything dies with the request.

**C5. Emergency stop does not cover browser or external actions.** It is checked
in `ActionExecutor` only. A `killed` state should mean *nothing* runs.

**C6. No audit resolution channel and no per-decision provenance of approval.**
Cheap to add, materially improves forensics.

### MEDIUM

**C7. No skill/plugin system.** Design it with declared capabilities and *loading
in a subprocess with a restricted profile* — not `importlib` in-process.

**C8. Model routing ignores cost, latency, privacy and context size.**

**C9. No context rules** (time windows, tool-name conditions).

**C10. No telemetry or self-observation.** We cannot see our own failure rates.

**C11. No goal/commitment model.** Blocks proactive behaviour entirely.

**C12. Windows desktop control is unverified.** Every Windows claim in this
project is still `SOURCE-ANALYZED — UNVERIFIED`.

### LOW

**C13. No voice.** **C14. No workflow engine** — I would not add one until
there is demand; it is a large surface whose only safe form is "a workflow node
is a tool call and goes through `ToolExecutor` like everything else."
**C15. No self-improvement loop.**

---

## D. Recommended architecture changes

Only the changes I would actually make, in the shape I would make them.

```
                    ┌─ GoalService ──────────── commitments, deadlines, follow-up
                    │
Orchestrator ───────┼─ AgentSupervisor ──────── spawn / cancel / budget / timeout
   (unchanged)      │        │
                    │        └─ child agents, capability set ⊂ parent's
                    │
                    ├─ BackgroundRunner ─────── progress, pause, resume, limits
                    │
                    └─ ToolExecutor ─────────── UNCHANGED as the only path
                             │
                             ├─ PermissionEngine  + context rules, + impact
                             ├─ ConfirmationService + channel, + deferred
                             └─ handlers
                                    │
                     ┌──────────────┴───────────────┐
                     │                              │
              ComputerService                 BrowserService
                     │                              │
              + UIA accessibility            (unchanged — Step 12)
                     │
              ┌──────┴──────┐
              │ OS SANDBOX  │  ← new: restricted user, egress filter
              └─────────────┘
```

Four invariants that must not move:

1. `ToolExecutor` stays the only path to a handler. Agents, background jobs and
   skills all call *through* it — none gets a private route.
2. A child's capability set is a strict subset of its parent's, computed at
   spawn, never widened at runtime.
3. DENY stays unconditional. We do not adopt `vierisid`'s grant-before-deny
   ordering.
4. Emergency stop moves *up* to the executor so it covers everything.

---

## E. Implementation roadmap

Ordered by dependency and by risk-reduction per unit of work.

| # | Change | Depends on | Risk |
|---|---|---|---|
| 1 | **Fix C2** — thread `ctx.tool_taint` into ambient capture; provenance on the row | — | low |
| 2 | Memory provenance + taint gate on all memory writes | 1 | low |
| 3 | Emergency stop moves to `ToolExecutor`; covers all capabilities | — | low |
| 4 | Audit `ResolutionChannel` + impact classification on confirmations | — | low |
| 5 | Context rules in `PermissionEngine` (time window, tool name) | — | low |
| 6 | `AgentSupervisor`: spawn/cancel, capability subsetting, budgets | 3 | **high** |
| 7 | Background execution: job table, progress, pause/resume/cancel, limits | 6 | high |
| 8 | OS sandbox for the terminal executor (Linux first, Windows second) | — | high, platform |
| 9 | Windows UIA accessibility layer for `ComputerService` | — | medium |
| 10 | Skill system, subprocess-isolated, manifest-declared capabilities | 6 | high |
| 11 | Model routing on cost/latency/privacy/context | — | low |
| 12 | Goal/commitment service | 7 | medium |
| 13 | Telemetry | — | low |
| 14 | Voice, with the destructive-never-by-voice gate built in from step one | 4 | medium |

Items 1–5 are small, independent, and reduce risk immediately. Item 6 is the
architectural fork — everything after it assumes agents exist.

---

## F. Security review — new attack surface, per change

| Change | New attack surface | Control |
|---|---|---|
| Sub-agents | Agent-to-agent privilege escalation; a compromised child asking a parent to act for it | Capability set computed at spawn as a strict subset; `spawn_agent` is itself gated; a child's request to a parent is a *tool call* subject to the parent's own checks, never a bypass |
| Background jobs | Runaway execution, resource exhaustion, actions taken while nobody is watching | Wall-clock and step budgets; a job inherits the taint state of its creator; anything needing confirmation *suspends* rather than proceeding unattended |
| Skills | Arbitrary code execution — the reference implementations both have this | Subprocess isolation, manifest-declared capabilities enforced by the host, no in-process `importlib`, signature or hash pinning before enable |
| OS sandbox | Privilege escalation in the provisioning code (it runs as root) | Fixed command vectors, no shell, validated inputs only — the reference does this correctly and is worth copying closely |
| UIA desktop control | A malicious window title or element name is untrusted text reaching the model | Same treatment as page content: framed, tainted, never instructions |
| Voice | Misheard approval; third parties in the room; audio from a video | Destructive never by voice; confidence floor; clarifier card |
| Goals | Proactive actions taken without a user present | Every goal action is still a tool call; nothing new is auto-allowed |
| Telemetry | Exfiltration of prompts, paths, secrets | Anonymous id, no content, explicit opt-in, and the existing redactor on the way out |

---

## G. Testing plan

Per subsystem, before it counts as production-ready:

- **Authority:** unauthorised rejected · low-risk accepted · medium/high gated ·
  LOCKDOWN rejects all non-essential · **a child cannot exceed its parent** ·
  a temporary grant cannot override a DENY (the `vierisid` defect, as a test).
- **Sub-agents:** spawn · capability subsetting · timeout · cancellation ·
  parent/child permission boundaries · a child cannot spawn above itself.
- **Background:** runaway detection · step and wall-clock limits · retry limits ·
  cancellation mid-action · escalation to a human · resume after restart.
- **Memory:** provenance preserved · tainted content cannot become a memory
  without confirmation · retrieval of the wrong memory rejected · sensitive data
  never written.
- **Computer:** correct window selected · action performed · **state verified
  after** · failure detected · timeout · emergency stop mid-chain.
- **Browser:** already covered (363 tests) — extend with agent-driven cases.
- **Skills:** a manifest declaring more than it is granted is refused · a skill
  cannot reach a tool it did not declare · subprocess isolation holds.
- **Audit:** every significant action produces a record, including refusals.

---

## H. Implementation status

Everything below is **NOT STARTED**. Nothing in this document has been built.

| # | Item | Status |
|---|---|---|
| 1 | C2 taint-laundering fix | **VERIFIED** — 6 tests, mutation-checked |
| 2 | Memory provenance + taint gate | **VERIFIED** — every write path audited; three further laundering paths found and closed |
| 3 | Emergency stop at the executor | **VERIFIED** — 7 tests |
| 4 | Audit channel + impact classification | **VERIFIED** — 8 tests, migration `a3f1d90e77b2`, destructive-never-by-voice enforced |
| 5 | Context rules | **VERIFIED** — 8 tests, no migration (grant conditions) |
| 6 | AgentSupervisor | **VERIFIED** — 29 tests. Wired to the orchestrator and exposed as `spawn_agent`; the delegated *model loop* is not built |
| 7 | Background execution | **IMPLEMENTED** — 18 tests + 3 tools. Lifecycle verified; a job currently *records* its request rather than carrying it out |
| 8 | OS sandbox | NOT STARTED |
| 9 | Windows UIA backend | **IMPLEMENTED — WINDOWS UNVERIFIED.** 14 tests off-Windows; no input call has ever executed |
| 10 | Skill system | NOT STARTED |
| 11 | Model routing | **IMPLEMENTED** — 5 tests. Constraints honoured; no caller passes them yet |
| 12 | Goals | NOT STARTED |
| 13 | Telemetry | **VERIFIED** — 13 tests, local-only by design, `/api/system/telemetry` |
| 14 | Voice | NOT STARTED |

Ten of fourteen. Nothing above is called VERIFIED unless a test exercises the
behaviour it claims — item 6 is IMPLEMENTED rather than VERIFIED because its
authority model is tested and its integration with a real agent loop is not,
and item 9 cannot be VERIFIED anywhere but Windows.

## I. Completion criteria

No item moves past IMPLEMENTED until it is integrated, tested,
security-reviewed, regression-tested and documented. JARVIS is not upgraded
because code was written.
