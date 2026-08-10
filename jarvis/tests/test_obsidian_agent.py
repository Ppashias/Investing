"""The Obsidian tools as the *agent* reaches them.

The existing Obsidian tests cover the transport, the provider, the service and
the HTTP routes. None of them executed a tool handler, which left the layer the
model actually touches unverified — a gap the pre-Phase-4 audit found.

Two levels here, and the distinction matters:

* **Through ToolExecutor** — the real permission engine, the real confirmation
  service, the real handler, the real vault. Proves each tool's behaviour.
* **Through the agent loop** — a scripted provider response asks for a tool,
  and the orchestrator, ExecuteStage, executor, permission engine,
  confirmation, handler, service, audit and result all run for real. Proves
  the *wiring*, including the ``extras`` the handler depends on.

Nothing calls a handler directly. A test that did would prove the function
works while saying nothing about whether the agent can reach it, which is
precisely the mistake being corrected.

Every vault here is a `tmp_path` fixture. Nothing touches a real vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from jarvis.core import JarvisCore
from jarvis.db.models import ActivityKind, ActivityLog, PermissionMode, ToolExecution
from jarvis.errors import ConfirmationRequiredError, PermissionDeniedError
from jarvis.knowledge.providers.obsidian import ObsidianService
from jarvis.tools.base import ToolContext
from jarvis.tools.executor import ToolCall

from .conftest import StubProvider, text_result, tool_result

NOTE = """---
tags: [languages]
aliases:
  - Ferris
---

# Rust

Ownership and borrowing are the core ideas.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """An isolated vault. Never a real one."""
    root = tmp_path / "AgentVault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "Notes").mkdir()
    (root / "Notes" / "Rust.md").write_text(NOTE, encoding="utf-8")
    (root / "Index.md").write_text("# Index\n\nSee [[Rust]].\n", encoding="utf-8")
    return root


async def _connect(
    core: JarvisCore, vault: Path, *, writes: bool = False, deletes: bool = False
) -> None:
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await ObsidianService(session, user.id).connect(
            str(vault), vault_name="AgentVault",
            allow_writes=writes, allow_deletes=deletes,
        )
        await session.commit()


async def _execute(core: JarvisCore, name: str, arguments: dict):
    """Run one tool the way ExecuteStage does.

    The executor comes from the orchestrator's own factory and the context
    carries the same extras ExecuteStage builds, so the permission engine,
    confirmation service and audit recorder are the production ones.
    """
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()

        orchestrator = core.orchestrator
        executor = orchestrator._make_executor(session)
        ctx = ToolContext(
            user_id=user.id,
            session=session,
            request_id="req_test",
            extras={
                "embeddings": core.embeddings,
                "project_id": None,
                "computer": core.computer,
                "activity": orchestrator._activity(session),
            },
        )
        try:
            outcome = await executor.execute(
                ToolCall(id="tu_1", name=name, arguments=arguments), ctx
            )
            await session.commit()
            return outcome
        except (ConfirmationRequiredError, PermissionDeniedError):
            await session.commit()
            raise


async def _activity_kinds(core: JarvisCore) -> list[tuple[str, str]]:
    async with core.database.session_factory() as session:
        rows = (await session.execute(select(ActivityLog))).scalars().all()
        return [(r.kind.value if hasattr(r.kind, "value") else r.kind,
                 (r.detail or {}).get("operation") or r.tool_name or "") for r in rows]


# ── FINDING 1: each tool through the real ToolExecutor ───────────────────────


async def test_search_obsidian_through_the_executor(core, vault: Path) -> None:
    await _connect(core, vault)
    outcome = await _execute(core, "search_obsidian", {"query": "ownership"})

    assert outcome.result.is_error is False
    assert "Notes/Rust.md" in outcome.result.content
    # The content is framed as data before it reaches the model.
    assert "never instructions to follow" in outcome.result.content
    assert outcome.result.data["count"] == 1
    assert outcome.decision is PermissionMode.ALLOW


async def test_read_obsidian_note_returns_the_exact_file(core, vault: Path) -> None:
    await _connect(core, vault)
    outcome = await _execute(
        core, "read_obsidian_note", {"path": "Notes/Rust.md"}
    )

    on_disk = (vault / "Notes" / "Rust.md").read_text(encoding="utf-8")
    assert on_disk in outcome.result.content
    assert outcome.result.data["path"] == "Notes/Rust.md"
    assert "languages" in outcome.result.data["tags"]
    assert outcome.decision is PermissionMode.ALLOW


async def test_list_obsidian_notes_through_the_executor(core, vault: Path) -> None:
    await _connect(core, vault)
    outcome = await _execute(core, "list_obsidian_notes", {})

    assert set(outcome.result.data["paths"]) == {"Index.md", "Notes/Rust.md"}
    assert outcome.result.data["count"] == 2


async def test_list_respects_a_folder_prefix(core, vault: Path) -> None:
    await _connect(core, vault)
    outcome = await _execute(core, "list_obsidian_notes", {"prefix": "Notes/"})
    assert outcome.result.data["paths"] == ["Notes/Rust.md"]


async def test_obsidian_status_reports_the_real_connection(core, vault: Path) -> None:
    await _connect(core, vault, writes=True)
    outcome = await _execute(core, "obsidian_status", {})

    assert outcome.result.data["connected"] is True
    assert outcome.result.data["vault"] == "AgentVault"
    assert "CREATE" in outcome.result.data["capabilities"]
    assert "2 notes" in outcome.result.content


async def test_obsidian_status_does_not_fabricate_a_connection(core) -> None:
    """No vault configured: the tool must say so rather than invent one."""
    outcome = await _execute(core, "obsidian_status", {})
    assert outcome.result.data["connected"] is False
    assert "No Obsidian vault is connected" in outcome.result.content


async def test_approval_does_not_override_the_write_switch(core, vault: Path) -> None:
    """A user approval is not a permission grant.

    The tool always asks first (``requires_confirmation=True``), so the
    approval comes *before* the operator switch is consulted. Approving it must
    still not produce a note when writes are switched off — the switch is a
    different question from consent, and this proves the two do not collapse.

    The ordering is a known wart, recorded in the hardening report: the user is
    asked about an operation that was always going to be refused. It is safe —
    nothing is written — but it is not good, and the fix is dynamic tool
    availability rather than anything in this layer.
    """
    await _connect(core, vault, writes=False)
    arguments = {"title": "Nope", "content": "x", "path": "Notes/Nope.md"}

    with pytest.raises(ConfirmationRequiredError) as caught:
        await _execute(core, "create_obsidian_note", arguments)

    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(
            caught.value.confirmation_id, approved=True
        )
        await session.commit()

    with pytest.raises(PermissionDeniedError, match="switched off"):
        await _execute(core, "create_obsidian_note", arguments)

    assert not (vault / "Notes" / "Nope.md").exists()


async def test_create_requires_confirmation_then_writes(core, vault: Path) -> None:
    """The full write gate, through the executor.

    Three separately meaningful assertions: the first attempt suspends, the
    file does *not* exist while it is suspended, and only after a real
    decision does the note appear on disk with the right content.
    """
    await _connect(core, vault, writes=True)
    arguments = {
        "title": "Decisions",
        "content": "# Decisions\n\nWe chose SQLite.",
        "path": "Notes/Decisions.md",
    }

    with pytest.raises(ConfirmationRequiredError) as caught:
        await _execute(core, "create_obsidian_note", arguments)
    confirmation_id = caught.value.confirmation_id

    assert not (vault / "Notes" / "Decisions.md").exists(), "wrote before approval"

    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(confirmation_id, approved=True)
        await session.commit()

    outcome = await _execute(core, "create_obsidian_note", arguments)
    assert outcome.result.is_error is False

    written = vault / "Notes" / "Decisions.md"
    assert written.is_file()
    body = written.read_text(encoding="utf-8")
    assert "We chose SQLite." in body
    assert "jarvis-created" in body


async def test_update_appends_without_destroying(core, vault: Path) -> None:
    await _connect(core, vault, writes=True)
    arguments = {
        "path": "Index.md",
        "content": "Appended by the agent.",
        "mode": "append",
    }

    with pytest.raises(ConfirmationRequiredError) as caught:
        await _execute(core, "update_obsidian_note", arguments)

    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(
            caught.value.confirmation_id, approved=True
        )
        await session.commit()

    await _execute(core, "update_obsidian_note", arguments)

    body = (vault / "Index.md").read_text(encoding="utf-8")
    assert "Appended by the agent." in body
    assert "See [[Rust]]." in body, "append destroyed the original"


async def test_update_is_denied_without_write_permission(core, vault: Path) -> None:
    await _connect(core, vault, writes=False)
    arguments = {"path": "Index.md", "content": "x", "mode": "append"}
    original = (vault / "Index.md").read_text(encoding="utf-8")

    with pytest.raises(ConfirmationRequiredError) as caught:
        await _execute(core, "update_obsidian_note", arguments)

    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(
            caught.value.confirmation_id, approved=True
        )
        await session.commit()

    with pytest.raises(PermissionDeniedError):
        await _execute(core, "update_obsidian_note", arguments)

    assert (vault / "Index.md").read_text(encoding="utf-8") == original


async def test_tools_report_no_vault_rather_than_crashing(core) -> None:
    """§22 at the tool layer: an absent vault is a state, not an exception."""
    for name, arguments in (
        ("search_obsidian", {"query": "anything"}),
        ("read_obsidian_note", {"path": "Any.md"}),
        ("list_obsidian_notes", {}),
    ):
        outcome = await _execute(core, name, arguments)
        assert outcome.result.is_error is True
        assert "No Obsidian vault is connected" in outcome.result.content


# ── FINDING 2: the OBSIDIAN_ACTION audit reaches agent operations ────────────


async def test_agent_read_produces_both_audit_records(core, vault: Path) -> None:
    """The gap this file exists to close.

    The executor's TOOL_CALL and PERMISSION_DECISION rows describe *that a tool
    ran*. The OBSIDIAN_ACTION row describes *what happened to the vault*. Both
    are wanted, and before the fix only the first two were written.
    """
    await _connect(core, vault)
    await _execute(core, "read_obsidian_note", {"path": "Notes/Rust.md"})

    kinds = await _activity_kinds(core)
    assert any(k == ActivityKind.TOOL_CALL.value for k, _ in kinds)
    assert any(k == ActivityKind.PERMISSION_DECISION.value for k, _ in kinds)

    obsidian = [op for k, op in kinds if k == ActivityKind.OBSIDIAN_ACTION.value]
    assert "read" in obsidian, f"no OBSIDIAN_ACTION read row; got {obsidian}"


async def test_audit_records_are_not_duplicated(core, vault: Path) -> None:
    await _connect(core, vault)
    await _execute(core, "search_obsidian", {"query": "ownership"})

    kinds = await _activity_kinds(core)
    searches = [
        op for k, op in kinds
        if k == ActivityKind.OBSIDIAN_ACTION.value and op == "search"
    ]
    assert len(searches) == 1, f"duplicated audit rows: {searches}"


async def test_a_denied_write_is_audited_as_denied(core, vault: Path) -> None:
    await _connect(core, vault, writes=False)
    arguments = {"title": "X", "content": "y"}

    with pytest.raises(ConfirmationRequiredError) as caught:
        await _execute(core, "create_obsidian_note", arguments)
    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(
            caught.value.confirmation_id, approved=True
        )
        await session.commit()

    with pytest.raises(PermissionDeniedError):
        await _execute(core, "create_obsidian_note", arguments)

    async with core.database.session_factory() as session:
        rows = (
            await session.execute(
                select(ActivityLog).where(
                    ActivityLog.kind == ActivityKind.OBSIDIAN_ACTION
                )
            )
        ).scalars().all()
    assert any(r.status == "DENIED" for r in rows), "a refusal left no trace"


async def test_the_obsidian_audit_endpoint_sees_agent_actions(
    core, client, vault: Path
) -> None:
    """End of the chain: the operation shows up where a user would look."""
    await _connect(core, vault)
    await _execute(core, "search_obsidian", {"query": "ownership"})

    entries = client.get("/api/obsidian/audit").json()["entries"]
    assert any(e["operation"] == "search" for e in entries)


async def test_the_orchestrator_supplies_the_activity_service(core) -> None:
    """Guards the wiring itself.

    ``_service`` degrades to no subject-specific audit when the key is absent,
    which is right for a hand-built context and wrong for the orchestrator.
    This asserts the orchestrator provides it, so the degradation path can
    never quietly become the normal one.
    """
    import inspect

    from jarvis.activity.service import ActivityService
    from jarvis.orchestrator.stages import ExecuteStage

    source = inspect.getsource(ExecuteStage._run_tools)
    assert '"activity": self.activity' in source

    async with core.database.session_factory() as session:
        assert isinstance(core.orchestrator._activity(session), ActivityService)


# ── FINDING 3: the complete chain, driven by a scripted provider ─────────────


async def _turn(core: JarvisCore, message: str):
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        await session.commit()
        return await core.orchestrator.handle(
            session=session, user=user, message=message
        )


async def test_agent_loop_reads_a_note_end_to_end(
    core, stub: StubProvider, vault: Path
) -> None:
    """Agent → Provider → ExecuteStage → ToolExecutor → Permission → Tool →
    Service → vault → Audit → Result → Agent.

    Nothing here calls a tool. The provider asks for one and the orchestrator
    does the rest, which is the only way to prove the wiring rather than the
    function.
    """
    await _connect(core, vault)
    stub.responses = [
        tool_result("read_obsidian_note", {"path": "Notes/Rust.md"}),
        text_result("Your note says ownership and borrowing are the core ideas."),
    ]

    response = await _turn(core, "What do my notes say about Rust ownership?")

    assert response.status == "completed"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["tool"] == "read_obsidian_note"
    assert response.tool_calls[0]["is_error"] is False
    assert "ownership" in response.text

    # The provider's second call must have been given the tool's output —
    # otherwise the answer is invented rather than grounded.
    second_request = stub.requests[-1]
    rendered = str(second_request.messages)
    assert "Ownership and borrowing" in rendered

    kinds = await _activity_kinds(core)
    assert any(
        k == ActivityKind.OBSIDIAN_ACTION.value and op == "read" for k, op in kinds
    )

    async with core.database.session_factory() as session:
        executions = (await session.execute(select(ToolExecution))).scalars().all()
    assert [e.tool_name for e in executions] == ["read_obsidian_note"]
    assert executions[0].permission_decision is PermissionMode.ALLOW


async def test_agent_loop_write_suspends_then_completes(
    core, stub: StubProvider, vault: Path
) -> None:
    """The same chain for a write, including the confirmation gate.

    The turn must *suspend* — not fail, not proceed — and the file must not
    exist until a human decides.
    """
    await _connect(core, vault, writes=True)
    arguments = {
        "title": "Meeting notes",
        "content": "# Meeting\n\nWe agreed to ship on Friday.",
        "path": "Notes/Meeting.md",
    }
    stub.responses = [
        tool_result("create_obsidian_note", arguments),
        text_result("Saved it to your vault."),
    ]

    first = await _turn(core, "Save the meeting notes to Obsidian")

    assert first.status == "needs_confirmation"
    assert first.pending_confirmation["tool"] == "create_obsidian_note"
    assert not (vault / "Notes" / "Meeting.md").exists(), "wrote before approval"

    confirmation_id = first.pending_confirmation["id"]
    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(confirmation_id, approved=True)
        await session.commit()

    stub.responses = [
        tool_result("create_obsidian_note", arguments),
        text_result("Saved it to your vault."),
    ]
    second = await _turn(core, "Save the meeting notes to Obsidian")

    assert second.status == "completed"
    written = vault / "Notes" / "Meeting.md"
    assert written.is_file()
    assert "ship on Friday" in written.read_text(encoding="utf-8")

    kinds = await _activity_kinds(core)
    assert any(
        k == ActivityKind.OBSIDIAN_ACTION.value and op == "create" for k, op in kinds
    )


async def test_agent_loop_denied_write_is_reported_not_performed(
    core, stub: StubProvider, vault: Path
) -> None:
    await _connect(core, vault, writes=False)
    stub.responses = [
        tool_result(
            "create_obsidian_note",
            {"title": "X", "content": "y", "path": "Notes/X.md"},
        ),
        text_result("I am not allowed to write to your vault."),
    ]

    first = await _turn(core, "Write a note")
    assert first.status == "needs_confirmation"

    async with core.database.session_factory() as session:
        from jarvis.confirmations.service import ConfirmationService

        await ConfirmationService(session).decide(
            first.pending_confirmation["id"], approved=True
        )
        await session.commit()

    stub.responses = [
        tool_result(
            "create_obsidian_note",
            {"title": "X", "content": "y", "path": "Notes/X.md"},
        ),
        text_result("I am not allowed to write to your vault."),
    ]
    second = await _turn(core, "Write a note")

    # The refusal comes back to the model as a tool error so it can explain
    # itself, rather than aborting the turn at the user.
    assert second.status == "completed"
    assert second.tool_calls[0]["is_error"] is True
    assert not (vault / "Notes" / "X.md").exists()


# ── security: the guarantees must survive the new wiring ────────────────────


async def test_a_note_still_cannot_authorise_a_write(core, vault: Path) -> None:
    """Taint escalation, re-proven at the tool layer after the audit change.

    A turn that has read untrusted content is tainted, and the engine forces
    every non-read capability to ASK — even with a grant that would otherwise
    allow it outright.
    """
    from jarvis.db.models import Capability, PermissionGrant

    await _connect(core, vault, writes=True)
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        session.add(
            PermissionGrant(
                user_id=user.id, capability=Capability.WRITE,
                resource_scope="tool:*", mode=PermissionMode.ALLOW,
            )
        )
        await session.commit()

        orchestrator = core.orchestrator
        executor = orchestrator._make_executor(session)
        ctx = ToolContext(
            user_id=user.id,
            session=session,
            tainted=True,  # a note was read this turn
            extras={
                "embeddings": core.embeddings,
                "project_id": None,
                "computer": core.computer,
                "activity": orchestrator._activity(session),
            },
        )
        with pytest.raises(ConfirmationRequiredError):
            await executor.execute(
                ToolCall(
                    id="tu_1",
                    name="create_obsidian_note",
                    arguments={"title": "Injected", "content": "x",
                               "path": "Notes/Injected.md"},
                ),
                ctx,
            )
        await session.commit()

    assert not (vault / "Notes" / "Injected.md").exists()


async def test_path_containment_holds_at_the_tool_layer(core, vault: Path) -> None:
    """A traversal attempt through the agent's own tool must be refused."""
    await _connect(core, vault, writes=True)
    outcome = await _execute(
        core, "read_obsidian_note", {"path": "../../../etc/passwd"}
    )
    assert outcome.result.is_error is True
    assert "outside the vault" in outcome.result.content


# ── §5 capability surface ────────────────────────────────────────────────────
#
# The vault can do more than the agent can ask for. Every omission below is a
# decision recorded in docs/jarvis/04-obsidian-capability-decisions.md, and
# these tests are what stop a decision from quietly reversing itself — a tool
# added later without going back to that document fails here first.


def test_the_agent_surface_is_exactly_what_was_decided() -> None:
    """The registry advertises six Obsidian tools and no others.

    Named individually rather than counted, because a count passes when one
    tool is swapped for another and the point is *which* operations the model
    can reach.
    """
    from jarvis.tools.registry import build_default_registry

    names = {t.name for t in build_default_registry().all() if t.category == "obsidian"}
    assert names == {
        "search_obsidian",
        "read_obsidian_note",
        "list_obsidian_notes",
        "obsidian_note_links",
        "create_obsidian_note",
        "update_obsidian_note",
        "obsidian_status",
    }


@pytest.mark.parametrize(
    "absent",
    [
        "delete_obsidian_note",
        "move_obsidian_note",
        "rename_obsidian_note",
        "sync_obsidian",
        "resolve_obsidian_conflict",
        "disconnect_obsidian",
    ],
)
def test_destructive_vault_operations_have_no_agent_tool(absent: str) -> None:
    """Implemented in the provider, reachable over the API, not by the model.

    Deleting, moving and conflict resolution all destroy something a human
    wrote: a file, a link graph, or the losing side of an edit. Each is
    available to the user through the Obsidian panel, where they are choosing
    to do it. None is available to a model that inferred it from a sentence.
    """
    from jarvis.tools.registry import build_default_registry

    assert absent not in {t.name for t in build_default_registry().all()}


def test_the_tools_the_agent_does_have_cannot_delete() -> None:
    """No Obsidian tool declares a capability above WRITE.

    The permission engine treats DELETE as its own capability; if a tool ever
    declared it, a WRITE grant would no longer describe the vault's exposure.
    """
    from jarvis.db.models import Capability
    from jarvis.tools.registry import build_default_registry

    obsidian = [t for t in build_default_registry().all() if t.category == "obsidian"]
    assert {t.capability for t in obsidian} <= {Capability.READ, Capability.WRITE}


async def test_backlinks_find_what_reading_a_note_cannot(core, vault: Path) -> None:
    """The reason this capability was exposed, stated as a test.

    ``Index.md`` links to ``Rust``. Nothing in ``Notes/Rust.md`` records that,
    so no amount of reading it reveals the reference — only a scan does.
    """
    await _connect(core, vault)
    outcome = await _execute(core, "obsidian_note_links", {"path": "Notes/Rust.md"})

    assert outcome.result.is_error is False
    assert outcome.result.data["backlinks"] == ["Index.md"]
    assert "Index.md" in outcome.result.content

    read = await _execute(core, "read_obsidian_note", {"path": "Notes/Rust.md"})
    assert "Index.md" not in read.result.content


async def test_backlinks_are_audited_as_a_read(core, vault: Path) -> None:
    await _connect(core, vault)
    await _execute(core, "obsidian_note_links", {"path": "Notes/Rust.md"})
    kinds = await _activity_kinds(core)
    assert ("OBSIDIAN_ACTION", "read") in kinds


async def test_backlinks_report_no_vault_rather_than_failing(core) -> None:
    outcome = await _execute(core, "obsidian_note_links", {"path": "Notes/Rust.md"})
    assert outcome.result.is_error is True
    assert outcome.result.data["connected"] is False


async def test_backlinks_cannot_escape_the_vault(core, vault: Path) -> None:
    await _connect(core, vault)
    outcome = await _execute(
        core, "obsidian_note_links", {"path": "../../../etc/passwd"}
    )
    assert outcome.result.is_error is True
    assert "outside the vault" in outcome.result.content


# ── §7 sync staleness ────────────────────────────────────────────────────────


async def test_status_says_the_index_has_never_been_synced(core, vault: Path) -> None:
    """Never-synced is a fact the model must be able to state.

    A search against an empty index returns nothing, which is indistinguishable
    from "you have no notes about that" unless something says otherwise.
    """
    await _connect(core, vault)
    outcome = await _execute(core, "obsidian_status", {})

    assert outcome.result.is_error is False
    assert outcome.result.data["last_synced_at"] is None
    assert "never been synced" in outcome.result.content
    assert "manual" in outcome.result.content


async def test_status_reports_how_stale_the_index_is(core, vault: Path) -> None:
    """After a sync, the age is reported — and so is the fact it is manual."""
    from datetime import timedelta

    from jarvis.db.base import utcnow

    await _connect(core, vault)
    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        service = ObsidianService(session, user.id)
        row = await service.row()
        row.last_synced_at = utcnow() - timedelta(days=3)
        row.document_count = 2
        await session.commit()

    outcome = await _execute(core, "obsidian_status", {})
    assert outcome.result.data["last_synced_at"] is not None
    assert outcome.result.data["indexed_documents"] == 2
    assert "3 day(s) ago" in outcome.result.content
    assert "does not sync by itself" in outcome.result.content


async def test_nothing_syncs_as_a_side_effect_of_reading(core, vault: Path) -> None:
    """Reading the vault must not quietly start an ingestion.

    A sync that happened because the user asked a question would be a
    background writer they did not authorise, and it would make the staleness
    reported above a lie.
    """
    await _connect(core, vault)
    await _execute(core, "read_obsidian_note", {"path": "Notes/Rust.md"})
    await _execute(core, "search_obsidian", {"query": "ownership"})
    await _execute(core, "list_obsidian_notes", {})

    async with core.database.session_factory() as session:
        user = await JarvisCore.ensure_default_user(session)
        row = await ObsidianService(session, user.id).row()
        assert row.last_synced_at is None
        assert row.document_count == 0


# ── §7 attachments ───────────────────────────────────────────────────────────


async def test_an_attachment_is_refused_not_decoded(core, vault: Path) -> None:
    """Reading a PNG must say "attachment", not return its bytes as prose.

    Before this, the transport decoded any file in the vault as UTF-8 with
    ``errors="replace"``, so asking to read an image produced a page of U+FFFD
    characters labelled as the note's content — a fabricated read the model had
    no way to recognise as one.
    """
    (vault / "Attachments").mkdir()
    (vault / "Attachments" / "diagram.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\xfd"
    )
    await _connect(core, vault)

    outcome = await _execute(
        core, "read_obsidian_note", {"path": "Attachments/diagram.png"}
    )
    assert outcome.result.is_error is True
    assert "attachment" in outcome.result.content
    assert "�" not in outcome.result.content


async def test_attachments_are_absent_from_listings_and_search(
    core, vault: Path
) -> None:
    """Skipping them is intentional; this pins it so it cannot drift."""
    (vault / "Attachments").mkdir()
    (vault / "Attachments" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (vault / "Attachments" / "notes.pdf").write_bytes(b"%PDF-1.7\n")
    await _connect(core, vault)

    listed = await _execute(core, "list_obsidian_notes", {})
    assert listed.result.data["paths"] == sorted(["Index.md", "Notes/Rust.md"])

    status = await _execute(core, "obsidian_status", {})
    assert "2 notes" in status.result.content


# ── §6 audit scope ───────────────────────────────────────────────────────────


def test_the_activity_log_has_no_user_column() -> None:
    """Why /api/obsidian/audit is not user-scoped, stated as a fact about the
    schema rather than an opinion about the route.

    The audit query cannot filter by user because there is nothing to filter
    on: ``activity_logs`` has no ``user_id``. JARVIS is single-user by
    construction (``ensure_default_user`` takes the first row), so the log
    describes one person's system. If a second subject ever exists, this test
    fails — which is the point, because the route would then be leaking.
    """
    from jarvis.db.models import ActivityLog

    assert "user_id" not in ActivityLog.__table__.columns
