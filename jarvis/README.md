# JARVIS — Core (Phases 1–2)

A personal AI operating system. Conversation, tasks, tools, permissions,
confirmation, observability, and — as of Phase 2 — persistent memory and an
ingested knowledge base, all behind a provider-independent AI layer.

**Computer control, browser control, and specialised agents are not built**, and
neither is the Obsidian connector: the architecture is ready for one and no
vault is reachable. The system says so plainly rather than pretending otherwise
— see [Not implemented](#not-implemented).

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
   └── MemoryEvaluator   after the answer: what was worth remembering?
   │
   ▼
SQLite (WAL) — 19 tables, Alembic migrations, vectors in the same file
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

**Not implemented.** `/api/knowledge/sources` reports it as `implemented: false`
and the UI shows it as *planned*.

What exists is the architecture: `SourceRef`/`ObsidianRef` can already represent
a vault id, note path, title, frontmatter, tags, links, backlinks, content hash
and sync status; all thirteen operations in the Phase 2.5 brief map onto the
existing `KnowledgeProvider` interface without extending it; and a test walks
that mapping so the interface cannot drift away from the contract. See
`knowledge/providers/obsidian_contract.py`, which is documentation and constants
with deliberately no implementation.

---

## Tools

Eleven. Nothing here can touch anything outside JARVIS's own store: no tool
declares `EXECUTE` or `EXTERNAL_ACTION`, and a test enforces it.

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
./.venv/bin/python -m pytest            # 296 tests
./.venv/bin/alembic revision --autogenerate -m "what changed"
./.venv/bin/alembic upgrade head
```

Tests use in-memory SQLite and a `StubProvider` test double, so they need no
API key and touch no real data. The stub lives in `tests/`, never in `src/`.

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
- **Two-way sync of any kind.** Nothing writes back to any external source.
- **File access on demand** (Phase 3) — ingestion reads from an allow-list;
  JARVIS cannot browse or write your filesystem.
- **Computer control** — screen, mouse, keyboard, applications (Phase 3/4)
- **Browser control** (Phase 5)
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
