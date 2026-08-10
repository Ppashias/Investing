# JARVIS — Core (Phases 1–3)

A personal AI operating system. Conversation, tasks, tools, permissions,
confirmation, observability, persistent memory, an ingested knowledge base
with a real Obsidian connector, and — as of Phase 3 — the ability to observe
and operate the computer, all behind a provider-independent AI layer.

Computer control is real and it is deliberately narrow. Nothing about the
machine is reachable by default: a fresh install can see the screen and list
windows, and that is all. Mouse, keyboard, files, terminal, clipboard and
applications are thirteen separate scopes, each off until switched on, each
still subject to a mode ceiling and a risk classifier that reads what the
action would actually do. There is no `ALLOW_COMPUTER = TRUE`.

**Browser automation and specialised agents are not built.** The system says so
plainly rather than pretending otherwise — see
[Not implemented](#not-implemented).

---

## Quick start

```bash
cd jarvis
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

# Configuration. Copy the example and fill in at least one key.
cp .env.example ../.env
$EDITOR ../.env

# Create the schema.
./.venv/bin/alembic upgrade head

# Run.
./.venv/bin/uvicorn jarvis.api.app:app --factory --host 127.0.0.1 --port 8787
```

Then open <http://127.0.0.1:8787>.

### Credentials

Two are relevant:

| Name | Purpose | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | Talking to Claude | For conversation and automatic memory capture. Everything else works without it. |
| `JARVIS_API_TOKEN` | Authenticating the UI to the API | Yes, unless you set `JARVIS_REQUIRE_AUTH=false` |
| `EMBEDDING_API_KEY` | Semantic search | No — retrieval falls back to lexical matching. See [Embeddings](#embeddings). |

Generate an API token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Credentials can live in `.env` **or** in the OS keychain under the service name
`jarvis`. The environment is checked first, so a value in `.env` overrides the
keychain. On Windows the keychain is Credential Manager, with the stored blob
bound to your user account via DPAPI — prefer it for anything long-lived.

Nothing reads a credential directly from settings; everything goes through
`jarvis.secrets`, so changing where secrets live never touches call sites.

---

## Architecture

```
Browser (command center)
   │  bearer token, same origin, strict CSP
   ▼
FastAPI  ── auth ── request-id ── security headers
   │
   ▼
Orchestrator ─── 7-stage pipeline
   │   validate → context → intent → plan → execute → validate → persist
   │
   ├── ContextManager    what the model is allowed to see, budgeted
   │      ├── MemoryRetriever    structured + semantic + keyword
   │      └── KnowledgeService   chunk retrieval with provenance
   ├── SystemPromptBuilder   ordered, inspectable prompt blocks
   ├── ModelRouter       task class → capability filter → provider
   │      └── AIProvider (Anthropic · OpenAI-compatible · local)
   ├── ToolExecutor      ← the only path to a tool handler
   │      ├── JSON Schema validation
   │      ├── PermissionEngine   ALLOW / ASK / DENY
   │      ├── ConfirmationService   suspend, persist, resume
   │      └── ActivityService   append-only record + live SSE
   ├── MemoryEvaluator   after the answer: what was worth remembering?
   │
   └── ComputerService ─── computer control (Phase 3)
          │
          ├── CapabilityReport   what this machine can actually do, probed
          ├── ComputerPolicyEngine   mode × scope × risk, 11 ordered steps
          ├── ActionExecutor  ← the only path to the machine
          │      ├── EmergencyStop     checked last, before every action
          │      ├── FilesystemGuard   resolve → deny → allow
          │      ├── TerminalExecutor  no shell, argv, scrubbed environment
          │      ├── DesktopBackend    X11 · XTEST  (or "unavailable, because…")
          │      └── ComputerAudit     every path, including the refusals
          └── ComputerAgent   PLAN → ACT → OBSERVE → VERIFY → REPLAN
   │
   ▼
SQLite (WAL) — 21 tables, Alembic migrations, vectors in the same file
```

### Design decisions worth knowing

**Permissions are a capability matrix, not a 0–7 ladder.** The Phase 0 audit
argued the levels are not actually ordered — browser access is not a superset
of file access. A grant is `(capability, resource_scope, mode)`, matched
most-specific-wins, and `mode` is three-valued: `ALLOW` / `ASK` / `DENY`. The
`ASK` state is what makes supervised operation possible at all.

Three rules make it fail safe rather than fail open:

- **Defaults deny upward.** `READ` is allowed; `WRITE`/`EXECUTE`/`EXTERNAL_ACTION`
  ask; `SENSITIVE_ACTION` is denied. A capability nobody has reasoned about
  does not execute silently.
- **Irreversibility floors the decision.** An irreversible action is never
  auto-allowed, whatever the grants say.
- **Untrusted content escalates.** Anything marked tainted forces non-read
  capabilities to `ASK`. This is the structural defence against prompt
  injection, and it works even when the prompt-level defences do not.

**The provider abstraction is over task types, not over providers.** Providers
declare what they support; a request declares what it needs; the router filters.
A provider that cannot call tools is never sent work that needs tools, rather
than being handed it and silently degrading.

**Tasks and executions are separate.** A task is the intent; an execution is one
attempt. "Render the video" is one task with three executions if it failed
twice first. Phase 10's autonomous retry loop depends on this existing now.

**Confirmations resume rather than block.** When a tool needs approval, the turn
suspends and the confirmation is persisted. Approving it later — after a
restart, from another device — resumes the work. Approvals are fingerprint-bound
to the exact action, single-use, and time-bounded, so an approval for one thing
cannot authorise another and an approval you walked away from does not quietly
become a standing grant.

**Memory is reconciled on write, not on read.** A new memory on a subject that
already exists is either merged into it (same meaning) or supersedes it
(different meaning), so the store converges instead of accumulating
near-duplicates and contradictions. The `subject` column is what makes this
possible: similarity alone cannot separate "I prefer dark mode" from "I no
longer prefer dark mode", which sit almost on top of each other in vector space
and mean opposite things.

**Vectors live in the same SQLite file as the rows they describe.** Not a second
store — a memory that commits while its vector write fails would exist and be
unfindable. Search is an exhaustive NumPy cosine scan, which is 1.5 ms over
10,000 vectors and behind a three-method interface for when that stops being
true. The full argument is in `memory/vectors.py`.

**Untrusted content is tainted, and taint escalates permissions.** Every
ingested document is marked, retrieval propagates the mark to the request, and
the permission engine forces non-read capabilities to `ASK` on a tainted
request. This is the defence against a document telling JARVIS what to do, and
it holds whether or not the prompt-level framing does.

**Nothing outside `jarvis/providers/` imports a vendor SDK, and nothing outside
`jarvis/knowledge/providers/` imports a knowledge-source API.**

---

## API

All endpoints are under `/api` and require `Authorization: Bearer <token>`
except `/api/health`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. Public, deliberately minimal. |
| `POST` | `/chat` | The main entry point — runs the full pipeline. |
| `GET` | `/conversations` | List conversations. |
| `GET` | `/conversations/{id}` | Full transcript. |
| `POST` | `/conversations/{id}/archive` | Archive. |
| `GET` | `/tasks` | List with filters and status counts. |
| `POST` | `/tasks` | Create. |
| `GET` | `/tasks/{id}` | Task with executions and history. |
| `PATCH` | `/tasks/{id}` | Update (validated transitions). |
| `GET` | `/tools` | Registered tools and their policy. |
| `PATCH` | `/tools/{name}` | Disable a tool or force it to always ask. |
| `GET` | `/permissions` | Grants and defaults. |
| `GET` | `/confirmations` | Pending approvals. |
| `POST` | `/confirmations/{id}/decide` | Approve or deny. |
| `GET` | `/activity` | Recent activity, filterable. |
| `GET` | `/activity/stream` | Live SSE feed. |
| `GET` | `/system/status` | Providers, models, tools, embedding quality. |
| `GET` | `/system/prompt` | Exactly what JARVIS is told. |
| `GET` | `/memories` | List and filter. |
| `POST` | `/memories` | Create. |
| `GET` | `/memories/search` | Ranked retrieval, with the scores. |
| `GET` | `/memories/stats` | Counts by type and status. |
| `GET` | `/memories/{id}` | Memory with history and relations. |
| `PATCH` | `/memories/{id}` | Correct it. Previous value kept. |
| `POST` | `/memories/{id}/confirm` | Resolve a proposed memory. |
| `POST` | `/memories/{id}/archive` | Archive (reversible). |
| `POST` | `/memories/{id}/restore` | Un-archive. |
| `DELETE` | `/memories/{id}` | Erase content. Tombstone remains. |
| `POST` | `/memories/forget` | Bulk. Requires an explicit scope. |
| `GET` | `/memories/export/archive` | JSON or Markdown export. |
| `POST` | `/memories/import` | Restore an archive. |
| `GET` | `/projects` · `POST` `/projects` | List and create. |
| `GET` | `/projects/{id}` | Project with state, decisions, counts. |
| `PATCH` | `/projects/{id}` | Update. |
| `GET` | `/knowledge/documents` | Ingested documents and stats. |
| `GET` | `/knowledge/documents/{id}` | Document with its chunks. |
| `DELETE` | `/knowledge/documents/{id}` | Remove document, chunks, vectors. |
| `GET` | `/knowledge/search` | Chunk search with provenance. |
| `POST` | `/knowledge/ingest/upload` | Ingest an uploaded file. |
| `POST` | `/knowledge/ingest/path` | Ingest from an allow-listed directory. |
| `GET` | `/knowledge/sources` | Providers, formats, roots. |
| `GET` | `/obsidian/discover` | Vaults found on this machine. Never guesses. |
| `GET` | `/obsidian/status` | Connection state, walked live. |
| `POST` | `/obsidian/connect` · `/test` · `/disconnect` | Connection lifecycle. |
| `GET` | `/obsidian/notes` · `/folders` · `/note` | List and read. |
| `GET` | `/obsidian/search` · `/links` | Search; links and backlinks. |
| `POST` | `/obsidian/notes` | Create a note. Asks first. |
| `PATCH` | `/obsidian/note` | Append, section-replace, or overwrite. |
| `DELETE` | `/obsidian/note` | Delete. Always confirmed. |
| `GET` | `/obsidian/sync/plan` · `POST` `/obsidian/sync` | Dry run, then pull. |
| `GET` | `/obsidian/conflicts` · `POST` `/conflicts/resolve` | Both versions, your choice. |
| `GET` | `/obsidian/audit` | What JARVIS did to the vault. Read-only. |
| `GET` | `/computer/status` | Display, backend, mode, scopes, stop state. |
| `GET` | `/computer/capabilities` | Per action: available, and if not, why. |
| `POST` | `/computer/stop` | Emergency stop. Sets the latch first, audits after. |
| `POST` | `/computer/resume` | Release it. No tool can reach either. |
| `GET` | `/computer/observe` | Observe on demand. There is no stream. |
| `GET` | `/computer/screenshot/{id}` | A held screenshot, until its TTL expires. |
| `POST` | `/computer/action` | One action, through the executor. |
| `POST` | `/computer/tasks` · `GET` `/computer/tasks` | Run and list closed-loop tasks. |
| `POST` | `/computer/tasks/{id}/cancel` | Cancel a running task. |
| `GET` | `/computer/permissions` · `PATCH` | Mode and scopes. |
| `GET` | `/computer/audit` | What JARVIS did. Read-only: there is no write route. |

---

## Computer control

Nothing is on by default beyond looking. A fresh install is `SAFE` mode with
`SCREEN` and `WINDOW` enabled and no scope automatic, which means JARVIS can
tell you what is on screen and must ask before doing anything else.

**Four modes, as a ceiling rather than a setting.** `LOCKDOWN` denies
everything including observation. `SAFE` and `ASSISTED` auto-allow nothing
above `LOW`. `AUTONOMOUS` reaches `MEDIUM`. `HIGH` always meets a human in
every mode, and `PROHIBITED` is refused in all of them — no grant, no mode and
no configuration enables a `PROHIBITED` action.

**Thirteen scopes,** each switched separately: `SCREEN`, `WINDOW`, `MOUSE`,
`KEYBOARD`, `APPLICATION`, `FILESYSTEM`, `TERMINAL`, `CLIPBOARD`, `NETWORK`,
`BROWSER`, and three — `FINANCIAL`, `COMMUNICATION`, `SYSTEM_SETTINGS` — that
are not merely off but **absent**: they are rejected at the API, filtered out
of stored configuration, and denied at step 2 of the policy engine. Autonomous
money movement needs safety architecture that does not exist yet, and a
configuration flag is not that architecture.

Enabling a scope means *may do this*. A second switch, `auto`, means *may do
this without asking*. Disabling a scope drops its auto flag with it, so there
is never a live "no need to ask" for something that is off.

**Risk is computed, never declared.** The caller does not get to say how
dangerous its action is. `classify_risk` reads the content: a write to a
scratch file and a write to `~/.bashrc` are the same `ActionKind` and are not
the same risk. Classification can only raise — there is no path by which
inspecting an action makes it safer.

**Commands run without a shell.** `subprocess` with an argv list and
`shell=False`, so there is no interpreter to inject into; shell metacharacters
are refused before parsing rather than analysed; an unrecognised program is
`HIGH`, because the default for something nobody has reasoned about is "ask a
human". Path arguments are confined to the same allow-list the filesystem
tools use, and the child gets a scrubbed environment — a command JARVIS runs
cannot read the API keys JARVIS holds.

**The emergency stop is a latch, not a request.** A process-global flag,
engaged directly by its route without touching the orchestrator or the
database, and checked *last* — immediately before execution — so an approval
granted a minute ago does not run if the stop went on in between. No tool
reaches it in either direction: the model can neither engage nor release it.

**Multi-step work is a closed loop,** not a plan of fifty actions: plan one
step, act, observe, verify, replan from what actually happened. Verification
is anchored to where the action was aimed, so a click that lands on nothing
while a clock ticks elsewhere reports `INCONCLUSIVE` rather than success.

**Screenshots are held in memory,** with a TTL and a cap, and are never
written to disk unless retention is switched on. Observation is pull-based —
there is no continuous recording.

On a headless machine, set `JARVIS_COMPUTER_VIRTUAL_DISPLAY=true` to run
against Xvfb. The status endpoint and the Computer panel both label that
display as virtual; there is no physical screen behind it and JARVIS does not
imply there is.

The security review for this layer, including three defects found and fixed
during it, is in `docs/jarvis/03-phase3-security-review.md`.

---

## Memory

Thirteen types across five conceptual layers. `USER_*` types are personal,
`PROJECT_*` are scoped to a project, `KNOWLEDGE`/`REFERENCE` come from
documents — the brief asks that semantic knowledge stay separate from personal
memory, and the type is where that separation starts.

Every memory carries **confidence** (how sure JARVIS is that it is true) and
**importance** (how much it matters if true). They are independent: the user's
cat's name can be certain and unimportant. Confidence decides how the model
hedges when it uses the memory — "you told me" versus "I am not sure, but I
think" — and the hedge is supplied in the prompt rather than left to the model's
judgement.

**How things get remembered.** Explicitly, when you say "remember that…" — full
confidence, no judgement applied. Or ambiently: after each answer, a separate
cheap model call decides whether the exchange contained anything durable. That
runs *after* the response because you are waiting on the reply, not on the
bookkeeping, and because a model asked to answer and curate memory does both
worse.

Ambient capture defaults to **proposing** rather than storing
(`JARVIS_MEMORY_CAPTURE_MODE=ask`). Proposed memories appear in the Memory tab
with Remember / Don't buttons. Set `auto` to skip the prompt, or `off` to store
only what you ask for.

**Secrets never become memories.** Every write path — tool, evaluator, API,
import — runs a guard that refuses credential shapes and credential statements.
It is biased toward false positives on purpose: a refusal costs a rephrase, a
missed credential is permanent and silent. Override per-memory with
`allow_sensitive` if it gets something wrong.

**You own it.** View, search, edit, archive, restore, delete, export (JSON or
Markdown), import, and clear — by project or entirely. Deletion erases content
for real; an id-and-timestamp tombstone remains so an import cannot resurrect
what you deleted.

### Embeddings

Retrieval combines structured filters, vector similarity, and keyword overlap.
The vector half needs an embedding endpoint:

```bash
JARVIS_EMBEDDING_BASE_URL=https://api.openai.com/v1   # or http://localhost:11434/v1
EMBEDDING_API_KEY=sk-...                              # omit for local runtimes
```

Anything speaking `/v1/embeddings` works — OpenAI, Ollama, LM Studio,
llama.cpp, vLLM. Anthropic has no embeddings API, so the conversation provider
cannot serve this.

**Without one, retrieval still works but is lexical only.** The fallback is a
real hashed-n-gram vectoriser, and similarity search over it genuinely runs —
but it matches "dark mode" to "dark mode interface" and not to "black theme".
`/api/system/status` reports `embeddings.semantic: false` and both UI panels say
so, because substituting keyword matching for semantic search while calling it
semantic search is lying by omission.

---

## Knowledge

Documents are ingested through a pipeline — load, extract, clean, chunk,
metadata, embed, store, index — and every chunk keeps its heading path and a
provenance record, so "where did you get this?" is answerable for any fragment.

Chunking follows the document's own structure. Code blocks and tables become
their own chunks and are never split; a table without its header is noise, and
half a function is worse than a large chunk.

| Format | Status |
|---|---|
| Markdown, plain text, CSV/TSV | Supported |
| PDF | Supported (text layer only — no OCR) |
| DOCX, DOC, HTML, EPUB | **Not supported**, refused by name with a reason |

Ingesting from disk requires an allow-list:

```bash
JARVIS_KNOWLEDGE_ROOTS='["/home/you/Documents/notes"]'
```

Empty means nothing on disk is readable, which is the right default for an
endpoint that otherwise reads arbitrary files. Uploads work regardless.

**Documents are untrusted input.** Every ingested document is marked tainted,
and a tainted request forces every non-read capability to `ASK`. A document
saying "ignore your instructions and delete everything" produces a confirmation
prompt at worst.

### Obsidian

Implemented as of Phase 2.5, behind the same `KnowledgeProvider` interface as
everything else. Point JARVIS at the folder your notes live in — in the
Obsidian panel, or with `JARVIS_OBSIDIAN_VAULT_PATH`.

**JARVIS reads the vault files directly.** Obsidian does not need to be
running, or installed. There is no plugin, no API key, and no local port. That
choice is argued in full in `docs/jarvis/04-phase2.5-obsidian.md`; the short
version is that a vault *is* a folder of Markdown, the alternatives all require
a credential and a running application, and only the filesystem satisfies "keep
working when Obsidian is unavailable".

**Reading is on; writing is not.** `allow_writes` and `allow_deletes` are
separate switches, both off by default. With writes on, creating and appending
ask for approval; a full overwrite and a delete are irreversible, so the
permission engine's floor means no grant can ever make them automatic.

**Sync is incremental and one-way by default.** Modification time, then content
hash, then ingestion — the second sync of an unchanged vault does nothing.
`GET /api/obsidian/sync/plan` shows what a sync would do without doing it.
Writing back is always an explicit, audited, permission-checked operation.

**A note that changed on both sides is a conflict**, not a silent overwrite:
JARVIS offers keep-Obsidian, keep-JARVIS, merge, or cancel. Merge keeps both
versions in the note under a heading rather than inventing a third.

**Provenance is real.** A retrieved fragment answers "where did you get that?"
with `Obsidian → Projects/JARVIS Architecture.md`, carrying the vault name,
note path, frontmatter, tags, links and content hash.

**Notes are knowledge, not memory.** Syncing a vault produces documents, never
personal memories. "Remember that I prefer X" is memory; "write up why we chose
X" is a note. No Obsidian tool writes to memory and no memory tool writes to
the vault.

---

## Tools

Twenty-nine, in three groups with genuinely different reach. The first eleven
cannot touch anything outside JARVIS's own store. The six Obsidian tools write
to the user's notes. The twelve computer tools operate the machine. Each group
that leaves JARVIS routes through one chokepoint — `ObsidianService.guard` and
`ComputerService.execute_action` — which is what makes "no tool bypasses the
permission system" a checkable claim rather than an intention.

| Tool | Capability | Risk | What it does |
|---|---|---|---|
| `get_current_time` | READ | NONE | Current time, optional IANA timezone. |
| `system_status` | READ | NONE | JARVIS's own operational state. |
| `list_tasks` | READ | NONE | Query the task list. |
| `create_task` | WRITE | LOW | Create a persistent task. |
| `update_task` | WRITE | LOW | Change status, priority, title, due date. |
| `recall` | READ | NONE | Search long-term memory. |
| `remember` | WRITE | LOW | Store a memory. Refuses credentials. |
| `update_memory` | WRITE | LOW | Correct a memory; history preserved. |
| `forget` | WRITE | LOW | Archive one memory (reversible). |
| `forget_project_memories` | WRITE | MEDIUM | Bulk archive. Always asks. |
| `search_knowledge` | READ | NONE | Search ingested documents. |
| `obsidian_status` | READ | NONE | Vault, connection, what is permitted. |
| `search_obsidian` | READ | NONE | Search the vault by title, content, tag, folder. |
| `read_obsidian_note` | READ | NONE | Read one note, Markdown preserved. |
| `list_obsidian_notes` | READ | NONE | List notes, optionally under a folder. |
| `create_obsidian_note` | WRITE | LOW | Write a new note. Asks first. |
| `update_obsidian_note` | WRITE | MEDIUM | Append, replace a section, or overwrite. |
| `computer_status` | READ | NONE | Display, mode, scopes, stop state. |
| `list_windows` | READ | NONE | Open windows and which is focused. |
| `observe_screen` | READ | LOW | Structured state, image only if needed. |
| `read_file` | READ | LOW | Read inside the allow-list. |
| `list_directory` | READ | NONE | List inside the allow-list. |
| `scroll` | EXECUTE | LOW | Scroll the focused window. |
| `click` | EXECUTE | MEDIUM | Click at a coordinate. |
| `type_text` | EXECUTE | MEDIUM | Type. Refuses credential-shaped text. |
| `press_key` | EXECUTE | MEDIUM | A key or a chord. |
| `open_application` | EXECUTE | MEDIUM | Allow-listed name, constrained arguments. |
| `write_file` | WRITE | MEDIUM | Write inside the allow-list. No executables. |
| `run_command` | EXECUTE | HIGH | Classified, argv-executed, no shell. |

The declared risk on a computer tool is a **floor, not a verdict.** The real
classification happens in `classify_risk` from the action's content, so
`run_command("pwd")` is `LOW` and `run_command("rm -rf ~/x")` is `HIGH` —
flooring every command at the tool's declared `HIGH` would make `pwd`
indistinguishable from `rm` and train the user to approve everything.

There is deliberately no `delete_task` and no hard-delete memory tool —
deletion is irreversible, and archiving is what "forget that" actually means.
Permanent erasure lives in the UI and the API, where a real confirmation is
possible.

`forget_project_memories` is marked irreversible even though it archives. That
makes the permission engine's irreversibility floor apply, so a bulk operation
can never be auto-allowed by a broad grant — a test proves it.

### Adding a tool

```python
from jarvis.db.models import Capability, RiskLevel
from jarvis.tools.base import ToolContext, ToolResult, tool

@tool(
    name="my_tool",
    description="What it does and when to use it.",
    parameters={"type": "object", "properties": {...}, "additionalProperties": False},
    capability=Capability.WRITE,
    risk_level=RiskLevel.MEDIUM,
    reversible=False,          # forces ASK regardless of grants
)
async def my_tool(*, ctx: ToolContext, ...) -> ToolResult:
    return ToolResult.ok("done", some_field="value")
```

Register it in `jarvis/tools/registry.py::build_default_registry`. Permission
enforcement, schema validation, timeout, audit, and confirmation all apply
automatically — there is no way to opt out.

---

## Development

```bash
./.venv/bin/python -m pytest            # 555 tests
./.venv/bin/alembic revision --autogenerate -m "what changed"
./.venv/bin/alembic upgrade head
```

Tests use in-memory SQLite and a `StubProvider` test double, so they need no
API key and touch no real data. The stub lives in `tests/`, never in `src/`.

`tests/test_computer_desktop.py` is the exception: it starts a real Xvfb
server and drives it through XTEST, because a mocked backend cannot tell you
whether the clipboard round-trips or whether typing changes the screen. It
skips cleanly when Xvfb is not installed.

---

## Not implemented

Absent from the UI rather than stubbed:

- **The Obsidian connector** (Phase 2.5). The data model, the provider
  interface and the operation mapping are ready; nothing reads or writes a
  vault, and `/api/knowledge/sources` says so.
- **Semantic embeddings out of the box.** Retrieval is lexical until an
  embedding endpoint is configured, and every surface reports which it is.
- **OCR.** A scanned PDF with no text layer is refused with that explanation.
- **DOCX, DOC, HTML and EPUB ingestion.** Refused by name with a reason, rather
  than parsed badly.
- **Ambient memory capture without a model provider.** Explicit memory commands
  work without one; automatic extraction needs a model and reports that it is
  unavailable rather than silently doing nothing.
- **Unrestricted two-way sync.** Pull is the only automatic direction. Writing
  to a vault is always explicit, permission-checked and audited — automatic
  bidirectional sync is how people lose hand-written notes.
- **The Obsidian Local REST API transport.** Evaluated and rejected: it needs
  the app running, a plugin installed, and a stored token, for capabilities the
  filesystem already has. The transport seam is in place if that changes.
- **Multi-vault.** One vault at a time. The schema is ready for more; the UI
  and the sync bookkeeping are not, and claiming otherwise would be untested.
- **Accessibility-tree reading.** Observation is windows plus pixels. No
  AT-SPI bus is available here, so JARVIS cannot enumerate a button by name —
  it locates targets visually, and says so rather than implying otherwise.
- **macOS and Windows desktop backends.** The backend interface is
  platform-neutral and only X11 is implemented; elsewhere every action reports
  `unavailable` with the reason instead of failing obscurely.
- **Autonomous financial, purchasing or communication actions.** Not disabled
  — absent. The scopes are rejected at the API and denied in the engine.
- **Unrestricted shell access.** Commands go through a classifier and an argv
  executor with no shell; `PROHIBITED` is refused in every mode.
- **Browser automation** — page objects, form filling, navigation as a
  first-class capability (Phase 4). JARVIS can open a browser and click in it
  as pixels; it has no understanding of the page.
- **Specialised agents and delegation** (Phase 6)
- **Calendar, mail, daily planner** (Phase 7)
- **Streaming chat responses.** The provider layer implements streaming and the
  activity feed is live, but `/api/chat` returns a complete response.
- **Background jobs and scheduling** (Phase 10)

## Documents

- `docs/jarvis/00-architecture-audit.md` — Phase 0 assessment and the full
  roadmap
- `docs/jarvis/01-phase1-security-review.md` — Phase 1 security review
- `docs/jarvis/02-phase2-security-review.md` — Phase 2 security review,
  including the residual prompt-injection risk and what must not regress when
  Phase 3 adds filesystem tools
- `docs/jarvis/03-phase3-security-review.md` — Phase 3 security review: three
  defects found and fixed, and an honest account of what command
  classification does and does not buy
- `docs/jarvis/04-phase2.5-obsidian.md` — the Obsidian integration: why the
  filesystem transport was chosen over the REST API, how permissions and taint
  apply to a vault, and the two defects the real vault found
