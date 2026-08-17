"""Structure-aware chunking (§25).

§25 says not to split every N characters, and the reason is retrieval quality:
a fixed-width window cuts sentences in half, separates a code block from the
paragraph explaining it, and strips a table of the header that gives its
columns meaning. Each of those produces a chunk that retrieves badly and reads
worse when it reaches the model.

The strategy here is **split on the document's own boundaries, then pack**:

1. Parse the document into structural blocks — headings, paragraphs, fenced
   code, tables, lists — carrying the heading path each one sits under.
2. Pack consecutive blocks of the same section into chunks up to a target size.
3. Never split an atomic block. A fenced code block or a table row group stays
   whole even when it exceeds the target, because half a function is worse than
   a large chunk.
4. Overlap by whole sentences, not characters, so a chunk boundary never lands
   mid-word.

Every chunk keeps its heading path, which is what makes a retrieved fragment
interpretable: "Architecture > Storage" from a design document means something,
where "400 characters from offset 8,000" does not.

Plain text has no markup to exploit, so it falls back to paragraph packing —
still a real boundary, just a weaker one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from jarvis.knowledge.types import ChunkKind

CHARS_PER_TOKEN = 4

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Block:
    """One structural unit of a document."""

    text: str
    kind: ChunkKind
    heading_path: list[str] = field(default_factory=list)
    #: Blocks that must not be split or merged across.
    atomic: bool = False
    char_start: int = 0
    char_end: int = 0


@dataclass(slots=True)
class Chunk:
    content: str
    kind: ChunkKind
    heading_path: str | None
    char_start: int
    char_end: int
    token_estimate: int
    metadata: dict[str, object] = field(default_factory=dict)


def parse_markdown_blocks(text: str) -> list[Block]:
    """Split Markdown into structural blocks, tracking the heading stack.

    Written by hand rather than with a Markdown library because the output
    needed is not an AST — it is source spans with heading context, which every
    library discards on the way to HTML.
    """
    blocks: list[Block] = []
    heading_stack: list[str] = []
    lines = text.splitlines(keepends=True)

    offset = 0
    buffer: list[str] = []
    buffer_start = 0
    buffer_kind = ChunkKind.PROSE

    def flush() -> None:
        nonlocal buffer, buffer_start, buffer_kind
        if buffer:
            content = "".join(buffer).strip()
            if content:
                blocks.append(
                    Block(
                        text=content,
                        kind=buffer_kind,
                        heading_path=list(heading_stack),
                        char_start=buffer_start,
                        char_end=buffer_start + len("".join(buffer)),
                    )
                )
        buffer = []
        buffer_kind = ChunkKind.PROSE

    index = 0
    # YAML frontmatter: kept as its own block so its keys stay searchable and
    # do not contaminate the first paragraph. Obsidian notes lead with it.
    if lines and lines[0].strip() == "---":
        for end in range(1, len(lines)):
            if lines[end].strip() in {"---", "..."}:
                raw = "".join(lines[: end + 1])
                blocks.append(
                    Block(
                        text=raw.strip(),
                        kind=ChunkKind.FRONTMATTER,
                        atomic=True,
                        char_start=0,
                        char_end=len(raw),
                    )
                )
                offset = len(raw)
                index = end + 1
                break

    while index < len(lines):
        line = lines[index]

        fence = _FENCE_RE.match(line.rstrip("\n"))
        if fence:
            flush()
            marker = fence.group(1)
            start = offset
            collected = [line]
            offset += len(line)
            index += 1
            while index < len(lines):
                collected.append(lines[index])
                offset += len(lines[index])
                closed = lines[index].rstrip("\n").startswith(marker)
                index += 1
                if closed:
                    break
            blocks.append(
                Block(
                    text="".join(collected).strip(),
                    kind=ChunkKind.CODE,
                    heading_path=list(heading_stack),
                    atomic=True,  # never split a code block
                    char_start=start,
                    char_end=offset,
                )
            )
            continue

        heading = _HEADING_RE.match(line.rstrip("\n"))
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            del heading_stack[level - 1 :]
            heading_stack.append(title)
            blocks.append(
                Block(
                    text=line.strip(),
                    kind=ChunkKind.HEADING_SECTION,
                    heading_path=list(heading_stack),
                    char_start=offset,
                    char_end=offset + len(line),
                )
            )
            offset += len(line)
            index += 1
            continue

        if _TABLE_ROW_RE.match(line):
            flush()
            start = offset
            collected = []
            while index < len(lines) and _TABLE_ROW_RE.match(lines[index]):
                collected.append(lines[index])
                offset += len(lines[index])
                index += 1
            blocks.append(
                Block(
                    text="".join(collected).strip(),
                    kind=ChunkKind.TABLE,
                    heading_path=list(heading_stack),
                    atomic=True,  # a table without its header is meaningless
                    char_start=start,
                    char_end=offset,
                )
            )
            continue

        if not line.strip():
            flush()
            offset += len(line)
            index += 1
            continue

        if not buffer:
            buffer_start = offset
            buffer_kind = ChunkKind.LIST if _LIST_RE.match(line) else ChunkKind.PROSE
        buffer.append(line)
        offset += len(line)
        index += 1

    flush()
    return blocks


def parse_text_blocks(text: str) -> list[Block]:
    """Paragraph blocks for formats with no markup."""
    blocks: list[Block] = []
    offset = 0
    for para in re.split(r"\n\s*\n", text):
        stripped = para.strip()
        if stripped:
            start = text.find(stripped, offset)
            start = start if start >= 0 else offset
            blocks.append(
                Block(
                    text=stripped,
                    kind=ChunkKind.PROSE,
                    char_start=start,
                    char_end=start + len(stripped),
                )
            )
            offset = start + len(stripped)
    return blocks


def split_oversized(block: Block, target_chars: int) -> list[Block]:
    """Break a too-large prose block on sentence boundaries.

    Extracted PDF and DOCX text frequently arrives as one enormous paragraph
    with no blank lines at all, so paragraph packing alone would emit a single
    chunk for the whole document. Splitting on sentences keeps the boundary
    meaningful; a hard character cut is only reached for text with no sentence
    punctuation, where there is no better boundary to find.
    """
    if block.atomic or len(block.text) <= target_chars:
        return [block]

    pieces: list[Block] = []
    buffer: list[str] = []
    length = 0
    cursor = block.char_start

    def flush_piece() -> None:
        nonlocal buffer, length, cursor
        if not buffer:
            return
        text = " ".join(buffer).strip()
        pieces.append(
            Block(
                text=text,
                kind=block.kind,
                heading_path=list(block.heading_path),
                char_start=cursor,
                char_end=cursor + len(text),
            )
        )
        cursor += len(text)
        buffer = []
        length = 0

    for sentence in _SENTENCE_END_RE.split(block.text):
        if length + len(sentence) > target_chars and buffer:
            flush_piece()
        if len(sentence) > target_chars:
            # No sentence boundary to use. Fall back to a hard cut, which is
            # the one case §25 cannot avoid.
            for start in range(0, len(sentence), target_chars):
                buffer = [sentence[start : start + target_chars]]
                length = len(buffer[0])
                flush_piece()
            continue
        buffer.append(sentence)
        length += len(sentence)

    flush_piece()
    return pieces or [block]


def pack_blocks(
    blocks: Iterable[Block],
    *,
    target_chars: int = 1_400,
    overlap_chars: int = 160,
    max_chars: int = 4_000,
) -> list[Chunk]:
    """Pack blocks into chunks, respecting section and atomicity boundaries."""
    chunks: list[Chunk] = []
    current: list[Block] = []
    current_len = 0
    # Overlap has to stay a fraction of the chunk, or a short target produces
    # chunks that are mostly a copy of the previous one — which inflates the
    # index and makes near-duplicate hits crowd out real ones.
    overlap_chars = min(overlap_chars, max(0, target_chars // 4))

    def heading_of(group: list[Block]) -> str | None:
        for block in group:
            if block.heading_path:
                return " > ".join(block.heading_path)
        return None

    def emit(group: list[Block], carry: str = "") -> None:
        if not group:
            return
        body = "\n\n".join(b.text for b in group)
        content = f"{carry}\n\n{body}".strip() if carry else body
        kinds = {b.kind for b in group}
        # A mixed group is prose that happens to contain a list; a
        # single-kind group keeps its identity.
        kind = kinds.pop() if len(kinds) == 1 else ChunkKind.PROSE
        chunks.append(
            Chunk(
                content=content,
                kind=kind,
                heading_path=heading_of(group),
                char_start=group[0].char_start,
                char_end=group[-1].char_end,
                token_estimate=max(1, len(content) // CHARS_PER_TOKEN),
            )
        )

    def tail_overlap(group: list[Block]) -> str:
        """Last whole sentences of a group, up to the overlap budget.

        Sentence-aligned rather than character-aligned: a chunk that starts
        mid-word retrieves worse and reads as corrupted.
        """
        if overlap_chars <= 0 or not group:
            return ""
        text = group[-1].text
        if group[-1].kind in {ChunkKind.CODE, ChunkKind.TABLE}:
            return ""
        sentences = _SENTENCE_END_RE.split(text)
        carry: list[str] = []
        total = 0
        for sentence in reversed(sentences):
            if total + len(sentence) > overlap_chars:
                # No partial sentences, and no oversized one either: if the
                # trailing sentence does not fit the budget, the chunks simply
                # abut. Carrying it anyway would duplicate most of a short
                # chunk into the next one.
                break
            carry.insert(0, sentence)
            total += len(sentence)
        return " ".join(carry).strip()

    previous_heading: list[str] | None = None

    expanded: list[Block] = []
    for block in blocks:
        expanded.extend(split_oversized(block, target_chars))

    for block in expanded:
        # A new top-level section starts a new chunk: packing across an H1
        # merges two topics that the author deliberately separated.
        section_changed = (
            previous_heading is not None
            and block.heading_path[:1] != previous_heading[:1]
        )
        if current and (section_changed or current_len + len(block.text) > target_chars):
            carry = tail_overlap(current) if not section_changed else ""
            emit(current)
            current = []
            current_len = 0
            if carry:
                # Carry forward as a pseudo-block so the next chunk opens with
                # the sentence that ended the last one.
                current.append(
                    Block(text=carry, kind=ChunkKind.PROSE,
                          heading_path=block.heading_path,
                          char_start=block.char_start, char_end=block.char_start)
                )
                current_len = len(carry)

        if block.atomic:
            # Code, tables and frontmatter become their own chunk. Packing them
            # in with surrounding prose would erase the ``kind`` that tells
            # retrieval what the chunk is, and a table merged into a paragraph
            # reads as noise. Oversized ones are emitted whole rather than
            # truncated: half a function is worse than a large chunk.
            if current:
                emit(current)
                current = []
                current_len = 0
            emit([block])
            previous_heading = block.heading_path
            continue

        current.append(block)
        current_len += len(block.text)
        previous_heading = block.heading_path

    emit(current)

    # A heading whose section was emitted separately leaves a chunk that is
    # only the heading, which retrieves nothing useful on its own. Tested by
    # structure rather than by length: a genuinely short paragraph is still
    # content, and a length threshold would silently eat it.
    return [c for c in chunks if not _heading_only(c.content)]


def chunk_document(
    text: str,
    *,
    media_type: str | None = None,
    target_chars: int = 1_400,
    overlap_chars: int = 160,
) -> list[Chunk]:
    """Entry point: parse by format, then pack."""
    if not text.strip():
        return []

    markdown_like = media_type in {"text/markdown", "text/x-markdown"} or bool(
        _HEADING_RE.search(text) or _FENCE_RE.search(text)
    )
    blocks = parse_markdown_blocks(text) if markdown_like else parse_text_blocks(text)
    return pack_blocks(
        blocks, target_chars=target_chars, overlap_chars=overlap_chars
    )


def _heading_only(content: str) -> bool:
    """True when every non-blank line is a Markdown heading."""
    lines = [line for line in content.strip().splitlines() if line.strip()]
    return bool(lines) and all(_HEADING_RE.match(line.strip()) for line in lines)
