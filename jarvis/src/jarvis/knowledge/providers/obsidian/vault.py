"""The vault transport — direct filesystem access to an Obsidian vault (§4).

This is the only module in JARVIS that knows what an Obsidian vault looks like
on disk. Everything above it goes through
:class:`~jarvis.knowledge.providers.obsidian.provider.ObsidianProvider`, which
speaks the generic :class:`~jarvis.knowledge.base.KnowledgeProvider` interface.

## Why the filesystem, and not the Local REST API

The transport was left open in the Phase 2 contract precisely so this decision
could be made against a real machine rather than guessed. Four candidates were
considered:

============================  ==========================================
Local vault filesystem        **Chosen.** Works whether Obsidian is open
                              or closed, needs no credentials, no network
                              listener, and no plugin the user must
                              install. The vault format *is* files: a
                              vault is a directory of Markdown with an
                              optional ``.obsidian/`` config folder, so
                              reading it directly is reading the real
                              thing, not a proxy for it.
Obsidian Local REST API       Rejected for now. Requires the Obsidian
                              desktop app to be *running*, a
                              community plugin installed, and a bearer
                              token stored by JARVIS — three failure modes
                              and one credential, in exchange for
                              capabilities the filesystem already has. It
                              would also make §22 impossible: JARVIS is
                              supposed to keep working when Obsidian is
                              unavailable, and a transport that needs the
                              app running cannot do that.
An MCP Obsidian server        Rejected. Same dependency on the app or on
                              a second process, plus a protocol hop, and
                              this machine has no such server configured.
Obsidian URI / plugin API     Rejected. ``obsidian://`` is a one-way
                              command channel for a running desktop app.
                              It cannot read, cannot search, and returns
                              nothing.
============================  ==========================================

The interface is transport-shaped on purpose — ``VaultTransport`` is a class,
not a module of functions — so a REST transport can be added later without the
provider, the service, the tools, the API or the UI changing. That was the
point of the contract's "transport is undecided on purpose".

## Path safety

A note path arrives from a model, an API request, or a note's own frontmatter.
All three are untrusted. Every path goes through :meth:`VaultTransport.resolve`,
which does the same three things the Phase 3 filesystem guard does, in the same
order and for the same reasons:

1. Reject absolute paths and drive letters outright — a vault-relative path is
   the only kind that means anything here.
2. ``resolve()`` the joined path, collapsing ``..`` and following symlinks.
3. Require containment in the vault root, tested with ``relative_to`` on
   resolved paths rather than string prefixes.

``.obsidian/`` is refused as well: it holds the vault's own configuration,
including community-plugin settings, and nothing about a *knowledge* provider
needs to read or write it.

## Frontmatter

Parsed with ``yaml.safe_load``, never ``yaml.load``. Frontmatter is content
from a file anything can write, so a loader that can construct arbitrary Python
objects is a remote-code-execution primitive pointed at the user's notes
folder. ``safe_load`` builds only plain data.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from jarvis.errors import JarvisError
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Extensions treated as notes. Obsidian itself renders Markdown; canvas files
#: are JSON and have no prose to index.
NOTE_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})

#: Directories never read or written, wherever they appear in the vault.
#: ``.obsidian`` is the vault's configuration (including plugin settings);
#: ``.trash`` is Obsidian's own recycle bin, and indexing it would resurrect
#: deleted notes into search results.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".obsidian", ".trash", ".git", ".stfolder", ".stversions", "node_modules"}
)

MAX_NOTE_BYTES = 4 * 1024 * 1024

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
#: ``[[Note]]``, ``[[Note|alias]]``, ``[[Note#Heading]]``.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
#: Inline ``#tag`` — not inside a code fence, not a Markdown heading.
_INLINE_TAG_RE = re.compile(r"(?:(?<=\s)|\A)#([A-Za-z0-9_][\w/-]*)")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


class VaultError(JarvisError):
    code = "obsidian_vault_error"
    http_status = 400
    retryable = False

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message, user_message=user_message or message)


class NoteNotFound(VaultError):
    code = "obsidian_note_not_found"
    http_status = 404


@dataclass(slots=True)
class NoteMeta:
    """Everything about a note except its body.

    Separate from the body because listing a vault must not read every file:
    a 5,000-note vault is a few megabytes of Markdown, and ``list_notes`` is
    called to render a picker.
    """

    path: str
    title: str
    byte_size: int
    modified_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "bytes": self.byte_size,
            "modified_at": self.modified_at.isoformat(),
        }


@dataclass(slots=True)
class Note:
    """A note, read."""

    path: str
    title: str
    #: The complete file, frontmatter included. Never modified on read.
    raw: str
    #: The body with the frontmatter block removed.
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    byte_size: int = 0
    modified_at: datetime | None = None
    content_hash: str = ""

    @property
    def folder(self) -> str:
        parent = str(Path(self.path).parent)
        return "" if parent == "." else parent

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "title": self.title,
            "folder": self.folder,
            "frontmatter": self.frontmatter,
            "tags": self.tags,
            "aliases": self.aliases,
            "links": self.links,
            "bytes": self.byte_size,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "content_hash": self.content_hash,
        }
        if include_body:
            payload["content"] = self.raw
        return payload


@dataclass(slots=True)
class VaultInfo:
    name: str
    path: str
    #: True when ``.obsidian/`` is present — i.e. Obsidian has opened this
    #: directory at least once. A vault without it is still a perfectly
    #: readable folder of Markdown, and JARVIS says which it found rather than
    #: refusing.
    has_obsidian_config: bool
    note_count: int
    folder_count: int


class VaultTransport:
    """Direct filesystem access to one vault."""

    kind = "filesystem"

    def __init__(self, root: str | Path, *, name: str | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.name = name or self.root.name

    # ── connection ───────────────────────────────────────────────────────────

    def check(self) -> VaultInfo:
        """Verify the vault is reachable. Raises rather than returning False,
        because every caller wants the reason."""
        if not self.root.exists():
            raise VaultError(
                f"{self.root} does not exist",
                "That vault folder does not exist. Check the path, and note "
                "that a vault on another machine is not reachable from here.",
            )
        if not self.root.is_dir():
            raise VaultError(
                f"{self.root} is not a directory",
                "That path is a file, not a vault folder.",
            )
        if not os.access(self.root, os.R_OK):
            raise VaultError(
                f"{self.root} is not readable",
                "That folder exists but cannot be read.",
            )

        notes = 0
        folders = set()
        for meta in self.iter_notes():
            notes += 1
            parent = str(Path(meta.path).parent)
            if parent != ".":
                folders.add(parent)

        return VaultInfo(
            name=self.name,
            path=str(self.root),
            has_obsidian_config=(self.root / ".obsidian").is_dir(),
            note_count=notes,
            folder_count=len(folders),
        )

    @property
    def writable(self) -> bool:
        return os.access(self.root, os.W_OK)

    # ── paths ────────────────────────────────────────────────────────────────

    def resolve(self, note_path: str) -> Path:
        """Vault-relative path → absolute path inside the vault.

        The single choke point for path safety. Resolve first, then contain —
        string comparison admits ``notes/../../.ssh/id_rsa`` and unresolved
        comparison admits a symlink pointing out of the vault.

        A **leading slash is treated as vault-root-relative**, not as the
        filesystem root, which is what it means inside Obsidian and what a
        person typing ``/JARVIS/Overview.md`` intends. ``/etc/passwd`` is
        therefore the note ``etc/passwd`` inside the vault — it does not
        exist, and the containment check makes sure it cannot become the real
        one. Windows drive letters are rejected outright because there is no
        reading of ``C:\\Windows`` that means a note.
        """
        raw = str(note_path).strip()
        if not raw:
            raise VaultError("Empty note path", "That note path is empty.")

        # Normalised before inspection: a Unicode look-alike separator or a
        # decomposed character must not slip a check that compares text.
        raw = unicodedata.normalize("NFC", raw).replace("\\", "/").lstrip("/")

        if len(raw) > 1 and raw[1] == ":":
            raise VaultError(
                f"Drive-qualified path rejected: {raw}",
                "Note paths are relative to the vault root.",
            )
        candidate = Path(raw)

        resolved = (self.root / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            raise VaultError(
                f"{resolved} is outside the vault",
                "That path is outside the vault.",
            ) from None

        for part in relative.parts:
            if part in EXCLUDED_DIRS:
                raise VaultError(
                    f"{part} is excluded",
                    f"{part}/ is Obsidian's own storage and is not accessible.",
                )
        return resolved

    def relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root)).replace(os.sep, "/")

    # ── reading ──────────────────────────────────────────────────────────────

    def iter_notes(self) -> Iterator[NoteMeta]:
        """Walk the vault, cheaply.

        ``os.walk`` rather than ``rglob`` so excluded directories can be pruned
        from the traversal instead of filtered afterwards — a vault with a
        200 MB ``.git`` should not be walked to find out it is ignored.
        """
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                if Path(filename).suffix.lower() not in NOTE_SUFFIXES:
                    continue
                if filename.startswith("."):
                    continue
                full = Path(dirpath) / filename
                try:
                    # A symlinked note can point anywhere; the resolved target
                    # has to be inside the vault or it is somebody else's file.
                    resolved = full.resolve()
                    resolved.relative_to(self.root)
                    stat = full.stat()
                except (OSError, ValueError):
                    log.debug("obsidian_note_skipped", path=str(full))
                    continue
                yield NoteMeta(
                    path=self.relative(full),
                    title=full.stem,
                    byte_size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )

    def list_notes(self, *, prefix: str | None = None, limit: int = 500) -> list[NoteMeta]:
        needle = (prefix or "").strip().lower().replace("\\", "/").lstrip("/")
        out: list[NoteMeta] = []
        for meta in self.iter_notes():
            if needle and not meta.path.lower().startswith(needle):
                continue
            out.append(meta)
            if len(out) >= limit:
                break
        return sorted(out, key=lambda m: m.path)

    def list_folders(self) -> list[str]:
        folders: set[str] = set()
        for dirpath, dirnames, _ in os.walk(self.root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")
            ]
            if Path(dirpath) == self.root:
                continue
            folders.add(self.relative(Path(dirpath)))
        return sorted(folders)

    def read(self, note_path: str) -> Note:
        path = self.resolve(note_path)
        if not path.is_file():
            raise NoteNotFound(
                f"{note_path} does not exist",
                f"There is no note at {note_path} in this vault.",
            )
        if path.suffix.lower() not in NOTE_SUFFIXES:
            # A vault holds attachments as well as notes — images, PDFs,
            # audio, whatever was dragged into it. Without this, reading one
            # decoded its bytes as UTF-8 with errors="replace" and handed the
            # caller a page of replacement characters *presented as the note's
            # content*. Silently returning mojibake is worse than failing:
            # a model shown that has no way to tell it apart from a note the
            # user actually wrote badly. JARVIS does not read attachments; it
            # now says so instead of pretending.
            raise VaultError(
                f"{note_path} has suffix {path.suffix!r}",
                f"{note_path} is an attachment, not a Markdown note. JARVIS "
                "reads and writes notes (.md); it does not open images, PDFs "
                "or other attachments in the vault.",
            )
        stat = path.stat()
        if stat.st_size > MAX_NOTE_BYTES:
            raise VaultError(
                f"{note_path} is {stat.st_size} bytes",
                f"{note_path} is larger than the {MAX_NOTE_BYTES // 1_048_576} MB "
                "note limit.",
            )

        data = path.read_bytes()
        # errors="replace" rather than a hard failure: one bad byte in a note
        # should not make the note unreadable, and the replacement character is
        # visible to whoever looks.
        raw = data.decode("utf-8", errors="replace")
        return self.parse(raw, path=self.relative(path), title=path.stem,
                          byte_size=len(data),
                          modified_at=datetime.fromtimestamp(
                              stat.st_mtime, tz=timezone.utc))

    def parse(
        self,
        raw: str,
        *,
        path: str,
        title: str,
        byte_size: int | None = None,
        modified_at: datetime | None = None,
    ) -> Note:
        frontmatter, body = split_frontmatter(raw)
        return Note(
            path=path,
            title=str(frontmatter.get("title") or title),
            raw=raw,
            body=body,
            frontmatter=frontmatter,
            tags=extract_tags(frontmatter, body),
            aliases=_as_list(frontmatter.get("aliases") or frontmatter.get("alias")),
            links=extract_links(body),
            byte_size=byte_size if byte_size is not None else len(raw.encode()),
            modified_at=modified_at,
            content_hash=content_hash(raw),
        )

    def exists(self, note_path: str) -> bool:
        try:
            return self.resolve(note_path).is_file()
        except VaultError:
            return False

    # ── searching ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        tag: str | None = None,
        folder: str | None = None,
        titles_only: bool = False,
    ) -> list[tuple[NoteMeta, float, str]]:
        """Search the vault. Returns ``(meta, score, excerpt)``.

        Obsidian's own index is a proprietary structure inside ``.obsidian``
        that is not documented, not stable across versions, and is not written
        at all unless the app has opened the vault. So this scans — but it
        scans in a bounded way: title and path match without reading a file at
        all, and content matching reads notes lazily and stops at the limit.
        For the semantic half of the story the vault is *ingested*, and that
        index is JARVIS's own.
        """
        terms = [t for t in re.split(r"\s+", query.strip().lower()) if t]
        if not terms:
            return []

        folder_needle = (folder or "").strip().strip("/").lower()
        results: list[tuple[NoteMeta, float, str]] = []

        for meta in self.iter_notes():
            if folder_needle and not meta.path.lower().startswith(folder_needle + "/"):
                continue

            haystack_path = meta.path.lower()
            title_hits = sum(1 for t in terms if t in meta.title.lower())
            path_hits = sum(1 for t in terms if t in haystack_path)

            score = 0.0
            excerpt = ""
            if title_hits:
                score += 2.0 * (title_hits / len(terms))
            if path_hits:
                score += 0.5 * (path_hits / len(terms))

            if not titles_only or tag:
                try:
                    note = self.read(meta.path)
                except VaultError:
                    continue

                if tag:
                    wanted = tag.strip().lstrip("#").lower()
                    if wanted not in {t.lower() for t in note.tags}:
                        continue
                    score += 1.0

                if not titles_only:
                    lowered = note.body.lower()
                    body_hits = sum(1 for t in terms if t in lowered)
                    if body_hits:
                        score += 1.5 * (body_hits / len(terms))
                        excerpt = _excerpt(note.body, terms)

            if score > 0:
                results.append((meta, score, excerpt))

        results.sort(key=lambda row: (-row[1], row[0].path))
        return results[:limit]

    # ── writing ──────────────────────────────────────────────────────────────

    def create(
        self,
        note_path: str,
        content: str,
        *,
        frontmatter: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> Note:
        path = self._writable_path(note_path)
        if path.exists() and not overwrite:
            raise VaultError(
                f"{note_path} already exists",
                f"{note_path} already exists. Updating it is a different "
                "operation from creating it, and needs your approval.",
            )

        raw = compose(content, frontmatter or {})
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, raw)
        log.info("obsidian_note_created", path=self.relative(path),
                 bytes=len(raw.encode()))
        return self.read(self.relative(path))

    def update(
        self,
        note_path: str,
        *,
        content: str | None = None,
        append: str | None = None,
        section: str | None = None,
        frontmatter: dict[str, Any] | None = None,
        expected_hash: str | None = None,
    ) -> Note:
        """Modify a note. Four modes, none of which is "replace blindly".

        ``expected_hash`` is the optimistic-concurrency check (§24). When it is
        supplied and does not match, the note changed under JARVIS since it was
        read and the write is refused rather than applied — that refusal is
        what a conflict *is*, and resolving it is the caller's job.
        """
        existing = self.read(note_path)
        if expected_hash and existing.content_hash != expected_hash:
            raise ConflictError(
                f"{note_path} changed since it was read",
                f"{note_path} has been edited since JARVIS last read it. "
                "Nothing was written.",
                note_path=note_path,
                expected_hash=expected_hash,
                actual_hash=existing.content_hash,
            )

        merged_frontmatter = dict(existing.frontmatter)
        if frontmatter:
            merged_frontmatter.update(frontmatter)

        if append is not None:
            body = existing.body.rstrip("\n") + "\n\n" + append.strip() + "\n"
        elif section is not None and content is not None:
            body = _replace_section(existing.body, section, content)
        elif content is not None:
            body = content
        else:
            body = existing.body

        raw = compose(body, merged_frontmatter)
        path = self._writable_path(note_path)
        _atomic_write(path, raw)
        log.info("obsidian_note_updated", path=note_path,
                 mode="append" if append is not None
                 else "section" if section else "replace")
        return self.read(note_path)

    def delete(self, note_path: str) -> dict[str, Any]:
        path = self._writable_path(note_path)
        if not path.is_file():
            raise NoteNotFound(
                f"{note_path} does not exist",
                f"There is no note at {note_path}.",
            )
        size = path.stat().st_size
        path.unlink()
        log.warning("obsidian_note_deleted", path=note_path, bytes=size)
        return {"path": note_path, "bytes": size}

    def move(self, note_path: str, new_path: str) -> Note:
        source = self._writable_path(note_path)
        destination = self._writable_path(new_path)
        if not source.is_file():
            raise NoteNotFound(f"{note_path} does not exist",
                               f"There is no note at {note_path}.")
        if destination.exists():
            raise VaultError(
                f"{new_path} already exists",
                f"{new_path} already exists.",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return self.read(self.relative(destination))

    def backlinks(self, note_path: str) -> list[str]:
        """Notes linking to this one.

        Computed by scanning, because Obsidian's link graph lives in its own
        undocumented cache. The target matches on stem — ``[[Overview]]`` and
        ``[[JARVIS/Overview]]`` both point at the same note in Obsidian's
        resolution rules, which prefer the shortest unambiguous form.
        """
        target = Path(note_path).stem.lower()
        found: list[str] = []
        for meta in self.iter_notes():
            if meta.path == note_path:
                continue
            try:
                note = self.read(meta.path)
            except VaultError:
                continue
            for link in note.links:
                if Path(link).stem.lower() == target:
                    found.append(meta.path)
                    break
        return sorted(found)

    # ── internals ────────────────────────────────────────────────────────────

    def _writable_path(self, note_path: str) -> Path:
        if not self.writable:
            raise VaultError(
                f"{self.root} is not writable",
                "This vault is read-only on disk, so JARVIS cannot change it.",
            )
        path = self.resolve(note_path)
        if path.suffix.lower() not in NOTE_SUFFIXES:
            raise VaultError(
                f"{note_path} is not a Markdown note",
                "JARVIS only writes Markdown notes (.md) into a vault.",
            )
        return path


class ConflictError(VaultError):
    """The note changed under JARVIS between read and write (§24)."""

    code = "obsidian_conflict"
    http_status = 409

    def __init__(
        self,
        message: str,
        user_message: str,
        *,
        note_path: str,
        expected_hash: str | None,
        actual_hash: str | None,
    ) -> None:
        super().__init__(message, user_message)
        self.note_path = note_path
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash


# ── markdown helpers ─────────────────────────────────────────────────────────


def content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a note into (frontmatter, body).

    ``yaml.safe_load`` rather than ``yaml.load``: frontmatter comes from a file
    anything can write, and a loader that can instantiate Python objects turns
    a notes folder into an execution primitive. Malformed YAML degrades to an
    empty mapping with the block left in the body, because a note with a typo
    in its frontmatter should still be readable.
    """
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        log.debug("obsidian_frontmatter_unparseable", error=str(exc))
        return {}, raw

    if not isinstance(parsed, dict):
        return {}, raw
    return parsed, raw[match.end():]


def compose(body: str, frontmatter: dict[str, Any]) -> str:
    """Rebuild a note from body and frontmatter.

    ``sort_keys=False`` and ``allow_unicode=True`` so a round trip does not
    reorder the user's properties or escape their non-ASCII text — a diff full
    of gratuitous changes is how an integration loses trust.
    """
    body = body.lstrip("\n")
    if not frontmatter:
        return body if body.endswith("\n") or not body else body + "\n"

    rendered = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"---\n{rendered}\n---\n\n{body}".rstrip("\n") + "\n"


def extract_tags(frontmatter: dict[str, Any], body: str) -> list[str]:
    """Both kinds of Obsidian tag: the ``tags:`` property and inline ``#tag``."""
    tags = {
        str(t).strip().lstrip("#")
        for t in _as_list(frontmatter.get("tags") or frontmatter.get("tag"))
        if str(t).strip()
    }
    # Fenced code is stripped first: `#include <stdio.h>` is not a tag, and
    # a shell comment in an example is not either.
    without_code = _CODE_FENCE_RE.sub(" ", body)
    tags.update(m.group(1) for m in _INLINE_TAG_RE.finditer(without_code))
    return sorted(t for t in tags if t)


def extract_links(body: str) -> list[str]:
    seen: list[str] = []
    for match in _WIKILINK_RE.finditer(_CODE_FENCE_RE.sub(" ", body)):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def json_safe(value: Any) -> Any:
    """Make a parsed-YAML value storable in a JSON column.

    ``created: 2026-08-10`` is one of the most common properties in a real
    vault — every daily note has one — and YAML parses it to a
    :class:`datetime.date`, which ``json.dumps`` refuses. Without this, a
    single date in frontmatter fails the document insert and the note becomes
    permanently un-indexable.

    Applied only where frontmatter crosses into the database. :attr:`Note.
    frontmatter` keeps the native types, so :func:`compose` writes the user's
    properties back in the form they were written — a round trip must not turn
    ``created: 2026-08-10`` into ``created: '2026-08-10'``.
    """
    import datetime

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # ``tags: a, b`` and ``tags: a`` are both legal in Obsidian.
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _replace_section(body: str, heading: str, replacement: str) -> str:
    """Replace one ``## Heading`` section, leaving the rest of the note alone.

    §14 asks for section updates specifically so an edit does not have to be a
    whole-file overwrite. A section runs from its heading to the next heading
    of the same or higher level — which is what Obsidian's own outline treats
    as the section, and what a person means by "the Architecture section".
    """
    wanted = heading.strip().lstrip("#").strip().lower()
    lines = body.splitlines()

    start = None
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match and match.group(2).strip().lower() == wanted:
            start = index
            level = len(match.group(1))
            break

    if start is None:
        # Appending is the honest behaviour for a section that does not exist:
        # the alternative is silently doing nothing, or overwriting the note.
        return (
            body.rstrip("\n") + f"\n\n## {heading.strip()}\n\n"
            + replacement.strip() + "\n"
        )

    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break

    head = lines[:start + 1]
    tail = lines[end:]
    return "\n".join([*head, "", replacement.strip(), "", *tail]).rstrip("\n") + "\n"


def _excerpt(body: str, terms: list[str], *, width: int = 240) -> str:
    lowered = body.lower()
    position = min(
        (lowered.find(t) for t in terms if lowered.find(t) >= 0), default=-1
    )
    if position < 0:
        return body[:width].strip()
    start = max(0, position - width // 3)
    return ("…" if start else "") + body[start:start + width].strip() + "…"


def _atomic_write(path: Path, raw: str) -> None:
    """Write via a temporary file in the same directory, then rename.

    A partial write is how an integration destroys a note: the process dies
    mid-``write_text`` and what is left is half a document with no backup.
    ``os.replace`` is atomic within a filesystem, so a reader sees either the
    old note or the new one.
    """
    temporary = path.with_name(f".{path.name}.jarvis-tmp")
    try:
        temporary.write_text(raw, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
