"""Knowledge: ingestion, chunking, provenance, retrieval, provider isolation.

Plus §40's Obsidian-readiness section, which is the unusual one: it tests that
a connector *could* be built without redesigning anything, while proving that
none exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.db.models import Document, EmbeddingOwner
from jarvis.errors import NotFoundError, ValidationError
from jarvis.knowledge.base import KnowledgeProvider
from jarvis.knowledge.ingestion import loaders
from jarvis.knowledge.ingestion.chunking import chunk_document, parse_markdown_blocks
from jarvis.knowledge.ingestion.pipeline import (
    IngestionPipeline,
    IngestRequest,
    ingest_path,
)
from jarvis.knowledge.providers import obsidian_contract
from jarvis.knowledge.providers.internal import (
    InternalKnowledgeProvider,
    LocalFileProvider,
)
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.types import (
    ChunkKind,
    DocumentStatus,
    KnowledgeCapability,
    ObsidianRef,
    SourceKind,
    SourceRef,
    SyncStatus,
)
from jarvis.providers.embeddings import LexicalEmbeddingProvider

MARKDOWN = """---
title: JARVIS Architecture
jarvis-project: jarvis
---

# JARVIS Architecture

The system is local-first and single-user by design, which shapes every
storage decision that follows from it.

## Storage

Vectors live in SQLite as float32 blobs and are searched by brute force over
the whole set, which is fast enough at this scale.

```python
def cosine(a, b):
    return float(a @ b)
```

| Option | Verdict |
|---|---|
| LanceDB | second store |
| sqlite-vec | pre-1.0 |

## Retrieval

Hybrid ranking combines structured, semantic and keyword signals so each covers
the failure modes of the others.
"""


@pytest.fixture
def embeddings() -> LexicalEmbeddingProvider:
    return LexicalEmbeddingProvider()


@pytest.fixture
def pipeline(session, embeddings) -> IngestionPipeline:
    return IngestionPipeline(session, embeddings=embeddings, chunk_target_chars=400)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "sub").mkdir(parents=True)
    (root / "arch.md").write_text(MARKDOWN, encoding="utf-8")
    (root / "sub" / "notes.txt").write_text(
        "Project X is an Unreal Engine game about deep sea exploration.",
        encoding="utf-8",
    )
    (root / "team.csv").write_text("name,role\nMia,cat\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    return root


async def ingest_text(pipeline: IngestionPipeline, user_id: str, name: str, text: str):
    return await pipeline.ingest(
        IngestRequest(
            user_id=user_id, filename=name, data=text.encode("utf-8"),
            source_kind=SourceKind.UPLOAD,
        )
    )


# ── chunking (§25) ───────────────────────────────────────────────────────────


def test_markdown_blocks_track_the_heading_stack() -> None:
    blocks = parse_markdown_blocks(MARKDOWN)
    storage = [b for b in blocks if b.heading_path[-1:] == ["Storage"]]
    assert storage
    assert storage[0].heading_path == ["JARVIS Architecture", "Storage"]


def test_code_and_tables_become_their_own_chunks() -> None:
    chunks = chunk_document(MARKDOWN, media_type="text/markdown", target_chars=400)
    kinds = {c.kind for c in chunks}
    assert ChunkKind.CODE in kinds, "a code block must keep its identity"
    assert ChunkKind.TABLE in kinds, "a table must keep its identity"


def test_code_block_is_never_split() -> None:
    chunks = chunk_document(MARKDOWN, media_type="text/markdown", target_chars=60)
    code = [c for c in chunks if c.kind is ChunkKind.CODE]
    assert len(code) == 1
    assert "def cosine" in code[0].content and "return float" in code[0].content


def test_chunks_carry_their_heading_path() -> None:
    chunks = chunk_document(MARKDOWN, media_type="text/markdown", target_chars=400)
    assert any(c.heading_path and "Retrieval" in c.heading_path for c in chunks)


def test_frontmatter_is_isolated() -> None:
    chunks = chunk_document(MARKDOWN, media_type="text/markdown")
    front = [c for c in chunks if c.kind is ChunkKind.FRONTMATTER]
    assert len(front) == 1
    assert "jarvis-project" in front[0].content


def test_oversized_paragraph_is_split_on_sentences() -> None:
    """The normal shape of PDF-extracted text: no blank lines at all."""
    body = "This is a sentence about retrieval. " * 60
    chunks = chunk_document(body, media_type="text/plain", target_chars=400)
    assert len(chunks) > 1
    assert all(len(c.content) < 1000 for c in chunks)


def test_text_without_sentence_punctuation_still_splits() -> None:
    chunks = chunk_document("word " * 400, media_type="text/plain", target_chars=300)
    assert len(chunks) > 1


def test_heading_only_chunks_are_dropped() -> None:
    assert chunk_document("# Only A Heading\n\n## And Another\n") == []


def test_overlap_never_exceeds_the_chunk() -> None:
    text = "First one here.\n\nSecond one there.\n\nThird one everywhere."
    chunks = chunk_document(text, media_type="text/plain", target_chars=40)
    assert len(set(c.content for c in chunks)) == len(chunks), "no chunk is a copy"


# ── loaders (§23) ────────────────────────────────────────────────────────────


def test_available_formats_reports_reality() -> None:
    formats = {f["key"]: f for f in loaders.available_formats()}
    assert formats["markdown"]["available"] is True
    assert formats["csv"]["available"] is True
    # Unsupported formats are named with a reason, not silently omitted.
    assert formats["docx"]["available"] is False
    assert formats["docx"]["reason"]


def test_unsupported_format_is_refused_by_name() -> None:
    with pytest.raises(ValidationError) as exc:
        loaders.loader_for("report.docx")
    assert "docx" in str(exc.value.user_message)


def test_csv_becomes_a_markdown_table() -> None:
    loaded = loaders.CsvLoader().load(
        b"name,role\nMia,cat\nNebula,dashboard\n", filename="team.csv"
    )
    assert "| name | role |" in loaded.text
    assert loaded.metadata["row_count"] == 2


def test_csv_cells_cannot_forge_columns() -> None:
    loaded = loaders.CsvLoader().load(b"a,b\n1|2,3\n", filename="t.csv")
    assert r"1\|2" in loaded.text


def test_loader_strips_invisible_characters() -> None:
    """Zero-width and bidi characters can hide text inside a benign document."""
    text = loaders.sanitise("safe​text‮hidden﻿")
    assert text == "safetexthidden"


def test_loader_strips_control_characters() -> None:
    assert "\x00" not in loaders.sanitise("a\x00b")


def test_markdown_title_comes_from_frontmatter() -> None:
    loaded = loaders.MarkdownLoader().load(MARKDOWN.encode(), filename="x.md")
    assert loaded.title == "JARVIS Architecture"
    assert loaded.metadata["frontmatter"]["jarvis-project"] == "jarvis"


# ── ingestion (§24, §26) ─────────────────────────────────────────────────────


async def test_ingest_produces_chunks_and_embeddings(pipeline, user) -> None:
    result = await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    assert result.document.status is DocumentStatus.INDEXED
    assert result.chunks_created > 1
    assert result.chunks_embedded == result.chunks_created
    assert result.document.chunk_count == result.chunks_created


async def test_ingested_documents_are_tainted(pipeline, user) -> None:
    """Every document is untrusted input, whatever its source (§42)."""
    result = await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    assert result.document.tainted is True


async def test_provenance_is_recorded(pipeline, user) -> None:
    result = await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    ref = SourceRef.from_dict(result.document.source_ref)
    assert ref is not None
    assert ref.kind is SourceKind.UPLOAD
    assert ref.content_hash and ref.ingested_at


async def test_unchanged_reingest_is_skipped(pipeline, user) -> None:
    await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    second = await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    assert second.skipped_unchanged is True
    assert second.chunks_embedded == 0


async def test_changed_document_is_reindexed(pipeline, user, session) -> None:
    first = await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    before = first.chunks_created

    second = await ingest_text(
        pipeline, user.id, "arch.md", MARKDOWN + "\n\n## New section\n\nMore text.\n"
    )
    assert second.skipped_unchanged is False
    assert second.document.id == first.document.id, "same document, not a duplicate"
    assert second.chunks_created >= before


async def test_empty_document_is_refused(pipeline, user) -> None:
    with pytest.raises(ValidationError):
        await ingest_text(pipeline, user.id, "empty.md", "   ")


async def test_oversized_document_is_refused(session, embeddings, user) -> None:
    small = IngestionPipeline(session, embeddings=embeddings, max_bytes=32)
    with pytest.raises(ValidationError):
        await ingest_text(small, user.id, "big.md", "x" * 100)


async def test_delete_document_removes_chunks_and_vectors(
    pipeline, session, user, embeddings
) -> None:
    result = await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    chunk_ids = [c.id for c in await KnowledgeService(session).chunks(result.document.id)]
    assert chunk_ids

    await pipeline.delete_document(result.document)

    from jarvis.memory.vectors import SqliteVectorIndex

    hits = await SqliteVectorIndex(session).search(
        owner_kind=EmbeddingOwner.CHUNK,
        model=embeddings.info.model,
        query=await embeddings.embed_one("vectors sqlite"),
    )
    assert not ({h.owner_id for h in hits} & set(chunk_ids))


# ── path safety (§42) ────────────────────────────────────────────────────────


async def test_ingest_inside_the_allow_list_works(pipeline, user, vault) -> None:
    result = await ingest_path(
        pipeline, vault / "arch.md", user_id=user.id, allowed_roots=[vault]
    )
    assert result.document.status is DocumentStatus.INDEXED


async def test_ingest_outside_the_allow_list_is_refused(pipeline, user, vault) -> None:
    outside = vault.parent / "outside" / "secret.txt"
    with pytest.raises(ValidationError):
        await ingest_path(pipeline, outside, user_id=user.id, allowed_roots=[vault])


async def test_path_traversal_is_refused(pipeline, user, vault) -> None:
    traversal = vault / ".." / "outside" / "secret.txt"
    with pytest.raises(ValidationError):
        await ingest_path(pipeline, traversal, user_id=user.id, allowed_roots=[vault])


async def test_no_roots_means_nothing_is_ingestable(pipeline, user, vault) -> None:
    """The safe default for an endpoint that otherwise reads arbitrary files."""
    with pytest.raises(ValidationError):
        await ingest_path(pipeline, vault / "arch.md", user_id=user.id, allowed_roots=[])


async def test_symlink_escape_is_skipped_when_scanning(vault) -> None:
    escape = vault / "escape"
    try:
        escape.symlink_to(vault.parent / "outside")
    except OSError:  # pragma: no cover - platform without symlink permission
        pytest.skip("symlinks unavailable")

    items = await LocalFileProvider([vault]).list_items()
    assert not any("outside" in item.id for item in items)


# ── retrieval ────────────────────────────────────────────────────────────────


async def test_search_finds_the_right_chunk(pipeline, session, user, embeddings) -> None:
    await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    await session.commit()

    result = await KnowledgeService(session, embeddings=embeddings).search(
        user.id, "how are vectors stored", limit=3
    )
    assert result.hits
    assert "SQLite" in result.hits[0].chunk.content


async def test_hits_carry_a_citation(pipeline, session, user, embeddings) -> None:
    await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    await session.commit()
    result = await KnowledgeService(session, embeddings=embeddings).search(
        user.id, "retrieval ranking", limit=3
    )
    assert result.hits
    assert "JARVIS Architecture" in result.hits[0].citation()


async def test_prompt_block_labels_content_as_reference(
    pipeline, session, user, embeddings
) -> None:
    await ingest_text(pipeline, user.id, "arch.md", MARKDOWN)
    await session.commit()
    result = await KnowledgeService(session, embeddings=embeddings).search(
        user.id, "vectors", limit=2
    )
    block = result.as_prompt_block()
    assert block.startswith("["), "every fragment must be labelled with its source"


async def test_one_document_cannot_monopolise_results(
    pipeline, session, user, embeddings
) -> None:
    await ingest_text(pipeline, user.id, "big.md", MARKDOWN)
    await ingest_text(
        pipeline, user.id, "other.md",
        "# Other\n\nVectors are also mentioned here in another document.\n",
    )
    await session.commit()

    result = await KnowledgeService(session, embeddings=embeddings).search(
        user.id, "vectors", limit=4
    )
    per_doc: dict[str, int] = {}
    for hit in result.hits:
        per_doc[hit.document.id] = per_doc.get(hit.document.id, 0) + 1
    assert max(per_doc.values()) <= 2


async def test_search_is_scoped_to_the_owner(pipeline, session, user, embeddings) -> None:
    from jarvis.db.models import User

    other = User(name="other")
    session.add(other)
    await session.flush()

    await ingest_text(pipeline, other.id, "theirs.md", MARKDOWN)
    await session.commit()

    result = await KnowledgeService(session, embeddings=embeddings).search(
        user.id, "vectors"
    )
    assert result.hits == []


# ── provider abstraction (§22, §32) ──────────────────────────────────────────


async def test_capability_detection(session, user, vault) -> None:
    internal = InternalKnowledgeProvider(session, user.id)
    local = LocalFileProvider([vault])

    assert internal.supports(KnowledgeCapability.SEARCH)
    # Documents enter through ingestion, not through a provider write.
    assert not internal.supports(KnowledgeCapability.CREATE)
    assert local.supports(KnowledgeCapability.INGEST)
    assert not local.supports(KnowledgeCapability.DELETE)


async def test_unsupported_operation_raises_a_clear_error(session, user) -> None:
    """Not NotImplementedError — a decision the caller can report."""
    provider = InternalKnowledgeProvider(session, user.id)
    with pytest.raises(ValidationError) as exc:
        await provider.create(title="x", content="y")
    assert "does not support" in str(exc.value)


async def test_provider_registration_and_lookup(session, user, vault) -> None:
    service = KnowledgeService(session)
    service.register(InternalKnowledgeProvider(session, user.id))
    service.register(LocalFileProvider([vault]))

    assert {p.key for p in service.providers()} == {"internal", "local"}
    ingesters = service.providers_supporting(KnowledgeCapability.INGEST)
    assert [p.key for p in ingesters] == ["local"]

    with pytest.raises(NotFoundError):
        service.get_provider("notion")


async def test_provider_status_reports_real_state(session, user, vault) -> None:
    service = KnowledgeService(
        session, providers=[InternalKnowledgeProvider(session, user.id)]
    )
    rows = {r["key"]: r for r in await service.provider_status()}
    assert rows["internal"]["connected"] is True
    assert rows["internal"]["implemented"] is True


# ── Obsidian readiness (§38, §40, §43) ───────────────────────────────────────


def test_the_contract_module_stays_a_contract() -> None:
    """Phase 2.5 implemented the connector — in its own package.

    This module must remain specification: no provider class, no transport, no
    working code. The separation is what lets the contract be checked against
    the implementation rather than *being* the implementation.
    """
    assert obsidian_contract.IMPLEMENTED is True
    classes = [
        name for name in dir(obsidian_contract)
        if isinstance(getattr(obsidian_contract, name), type)
    ]
    assert not any("Provider" in name for name in classes)


async def test_obsidian_reports_implemented_but_unconnected(session, user, vault) -> None:
    """Two different facts, and the report keeps them apart.

    ``implemented`` is about the code existing; ``connected`` is about a vault
    being reachable. Collapsing them is what made the Phase 2 report useless —
    it could not distinguish "not built" from "not configured".
    """
    service = KnowledgeService(
        session,
        providers=[InternalKnowledgeProvider(session, user.id), LocalFileProvider([vault])],
    )
    assert "obsidian" not in {p.key for p in service.providers()}

    rows = {r["key"]: r for r in await service.provider_status()}
    assert rows["obsidian"]["implemented"] is True
    assert rows["obsidian"]["connected"] is False


def test_every_contract_operation_maps_to_an_interface_method() -> None:
    """If the interface ever loses a method the contract needs, fail now.

    This is the whole point of writing the contract in Phase 2: discovering a
    gap here costs an edit, discovering it in Phase 2.5 costs a migration.
    """
    for operation, method in obsidian_contract.OPERATION_MAP.items():
        assert hasattr(KnowledgeProvider, method), f"{operation} -> {method} missing"


def test_interface_can_express_every_expected_obsidian_capability() -> None:
    for capability in obsidian_contract.EXPECTED_OBSIDIAN_CAPABILITIES:
        assert isinstance(capability, KnowledgeCapability)


def test_obsidian_provenance_round_trips() -> None:
    """§38 — the schema must represent a note's full identity today."""
    ref = ObsidianRef(
        vault_id="vault-1",
        vault_name="Main",
        vault_path="/home/user/Vault",
        note_path="Projects/Project X.md",
        note_title="Project X",
        note_id="abc123",
        frontmatter={"jarvis-project": "project-x", "tags": "game"},
        tags=["game", "unreal"],
        links=["Engine Notes"],
        backlinks=["Index"],
        section="Architecture > Storage",
        content_hash="deadbeef",
        sync_status=SyncStatus.SYNCED,
    )
    restored = ObsidianRef.from_dict(ref.to_dict())

    assert restored.note_path == "Projects/Project X.md"
    assert restored.frontmatter["jarvis-project"] == "project-x"
    assert restored.links == ["Engine Notes"]
    assert restored.backlinks == ["Index"]
    assert restored.sync_status is SyncStatus.SYNCED


async def test_a_memory_can_carry_obsidian_provenance(session, user, embeddings) -> None:
    """The readiness proof: store and read back a vault-sourced memory with no
    connector in sight."""
    from jarvis.memory.service import MemoryDraft, MemoryService
    from jarvis.memory.types import MemorySource, MemoryType

    ref = SourceRef.from_obsidian(
        ObsidianRef(
            vault_name="Main",
            note_path="Projects/Project X.md",
            note_title="Project X",
            frontmatter={"jarvis-project": "project-x"},
            tags=["game"],
            backlinks=["Index"],
        )
    )
    outcome = await MemoryService(session, embeddings=embeddings).create(
        user.id,
        MemoryDraft(
            content="Project X targets Unreal Engine 5.8",
            type=MemoryType.PROJECT_FACT,
            source=MemorySource.OBSIDIAN,
            source_ref=ref,
        ),
    )

    stored = SourceRef.from_dict(outcome.memory.source_ref)
    assert stored is not None and stored.kind is SourceKind.OBSIDIAN
    assert stored.obsidian is not None
    assert stored.obsidian.note_path == "Projects/Project X.md"
    assert stored.obsidian.backlinks == ["Index"]
    # A vault is external content, so it is tainted like any document.
    assert outcome.memory.tainted is True


async def test_a_document_can_carry_obsidian_provenance(pipeline, user) -> None:
    ref = SourceRef.from_obsidian(
        ObsidianRef(note_path="Notes/Idea.md", note_title="Idea", vault_name="Main")
    )
    result = await pipeline.ingest(
        IngestRequest(
            user_id=user.id,
            filename="Idea.md",
            data=b"# Idea\n\nA thought worth keeping around for later reference.\n",
            source_kind=SourceKind.OBSIDIAN,
            source_ref=ref,
        )
    )
    stored = SourceRef.from_dict(result.document.source_ref)
    assert stored.kind is SourceKind.OBSIDIAN
    assert stored.obsidian.note_path == "Notes/Idea.md"


def test_only_the_connector_produces_an_obsidian_source() -> None:
    """``SourceKind.OBSIDIAN`` may only be set by the Obsidian code.

    In Phase 2 this asserted that *nothing* produced it. Now that a connector
    exists the useful property is narrower and still worth enforcing: a
    document claiming to come from a vault must have come through the vault
    package, not from some other code path that decided to label itself.
    """
    import subprocess

    root = Path(__file__).resolve().parents[1] / "src" / "jarvis"
    hits = subprocess.run(
        ["grep", "-rn", "SourceKind.OBSIDIAN", str(root)],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()

    allowed = (
        "knowledge/types.py", "knowledge/service.py",
        "providers/obsidian_contract.py", "providers/obsidian/",
    )
    for line in hits:
        assert any(a in line for a in allowed), f"unexpected producer: {line}"
