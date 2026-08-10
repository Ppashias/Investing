# JARVIS — Core (Phase 1)

The orchestration layer of a personal AI operating system. This is the JARVIS
core: conversation, tasks, tools, permissions, confirmation, and observability,
with a provider-independent AI layer.

**Phase 1 is the foundation, not the product.** Memory, computer control,
browser control, and specialised agents are not built. The system says so
plainly rather than pretending otherwise — see [Not implemented](#not-implemented).

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
| `ANTHROPIC_API_KEY` | Talking to Claude | For conversation. Everything else works without it. |
| `JARVIS_API_TOKEN` | Authenticating the UI to the API | Yes, unless you set `JARVIS_REQUIRE_AUTH=false` |

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
   ├── SystemPromptBuilder   ordered, inspectable prompt blocks
   ├── ModelRouter       task class → capability filter → provider
   │      └── AIProvider (Anthropic · OpenAI-compatible · local)
   └── ToolExecutor      ← the only path to a tool handler
          ├── JSON Schema validation
          ├── PermissionEngine   ALLOW / ASK / DENY
          ├── ConfirmationService   suspend, persist, resume
          └── ActivityService   append-only record + live SSE
   │
   ▼
SQLite (WAL) — 11 tables, Alembic migrations
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
to the exact action and single-use, so an approval for one thing cannot
authorise another.

**Nothing outside `jarvis/providers/` imports a vendor SDK.**

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
| `GET` | `/system/status` | Providers, models, tools. |
| `GET` | `/system/prompt` | Exactly what JARVIS is told. |

---

## Tools

Five, all safe. Phase 1 ships nothing irreversible and nothing above `LOW` risk;
a test enforces that.

| Tool | Capability | Risk | What it does |
|---|---|---|---|
| `get_current_time` | READ | NONE | Current time, optional IANA timezone. |
| `system_status` | READ | NONE | JARVIS's own operational state. |
| `list_tasks` | READ | NONE | Query the task list. |
| `create_task` | WRITE | LOW | Create a persistent task. |
| `update_task` | WRITE | LOW | Change status, priority, title, due date. |

There is deliberately no `delete_task` — deletion is irreversible; cancelling is
the supported operation.

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
./.venv/bin/python -m pytest            # 124 tests
./.venv/bin/alembic revision --autogenerate -m "what changed"
./.venv/bin/alembic upgrade head
```

Tests use in-memory SQLite and a `StubProvider` test double, so they need no
API key and touch no real data. The stub lives in `tests/`, never in `src/`.

---

## Not implemented

Phase 1 stops here on purpose. These are absent from the UI rather than stubbed:

- **Memory and semantic search** (Phase 2) — `ContextManager._load_memory`
  returns empty. It does not return placeholder text, because fabricated
  "remembered" content would make the model claim recall it does not have.
- **File access** (Phase 3)
- **Computer control** — screen, mouse, keyboard, applications (Phase 4)
- **Browser control** (Phase 5)
- **Specialised agents and delegation** (Phase 6)
- **Calendar, mail, projects, daily planner** (Phase 7)
- **Streaming chat responses.** The provider layer implements streaming and the
  activity feed is live, but `/api/chat` returns a complete response. Wiring
  token-level streaming through the pipeline is Phase 2 work.
- **Background jobs and scheduling** (Phase 10)

See `docs/jarvis/00-architecture-audit.md` for the full roadmap.
