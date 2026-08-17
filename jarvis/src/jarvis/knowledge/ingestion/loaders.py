"""Document loaders (§23).

§23 asks for the ingestion *architecture* plus a real subset of formats, and
explicitly not for every format for completeness' sake. So: four loaders that
work against the dependencies actually present, and a registry that reports
honestly which formats are available rather than accepting a file it cannot
read.

| Format | Loader | Dependency |
|---|---|---|
| Markdown | `MarkdownLoader` | none |
| Plain text | `TextLoader` | none |
| CSV/TSV | `CsvLoader` | stdlib |
| PDF | `PdfLoader` | `pypdf` |

DOCX and HTML are **not** implemented. Both would need a dependency that is not
installed, and a loader that silently produced mangled output would be worse
than a clear "unsupported": the failure would surface much later, as bad
retrieval, with no obvious cause. :func:`available_formats` reports what is
real, and the UI renders from it.

## Extraction is untrusted

Every loader returns text that came from a file JARVIS did not write. A PDF can
contain "ignore your previous instructions" as easily as a web page (§42).
Loaders therefore do two things: strip control characters that could break
prompt structure, and return content that the pipeline marks ``tainted``. No
loader interprets its input as instructions, and nothing downstream treats
extracted text as anything but data.
"""

from __future__ import annotations

import abc
import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from jarvis.errors import ValidationError
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Control characters, except tab/newline/carriage return. Zero-width and
#: bidirectional-override characters are stripped too: they are invisible in a
#: UI and can be used to hide text inside an otherwise innocuous document.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def sanitise(text: str) -> str:
    """Normalise and strip characters that have no business in a document.

    NFKC first, so visually identical characters compare equal downstream and a
    homoglyph cannot be used to slip past a filter that a reviewer would read
    as ordinary text.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_RE.sub("", text)
    text = _INVISIBLE_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(slots=True)
class LoadedDocument:
    text: str
    media_type: str
    title: str
    metadata: dict[str, object]


class DocumentLoader(abc.ABC):
    key: str = "loader"
    media_types: frozenset[str] = frozenset()
    extensions: frozenset[str] = frozenset()

    @classmethod
    def available(cls) -> bool:
        """False when a required dependency is missing."""
        return True

    @abc.abstractmethod
    def load(self, data: bytes, *, filename: str) -> LoadedDocument: ...

    @staticmethod
    def _decode(data: bytes) -> str:
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")


class MarkdownLoader(DocumentLoader):
    key = "markdown"
    media_types = frozenset({"text/markdown", "text/x-markdown"})
    extensions = frozenset({".md", ".markdown", ".mdx"})

    def load(self, data: bytes, *, filename: str) -> LoadedDocument:
        text = sanitise(self._decode(data))
        title, frontmatter = _markdown_title_and_frontmatter(text, filename)
        return LoadedDocument(
            text=text,
            media_type="text/markdown",
            title=title,
            # Frontmatter is preserved rather than parsed as YAML: no YAML
            # parser is installed, and yaml.load on untrusted input is a
            # deserialisation hazard even when one is. Key-value scanning
            # covers what the join keys need.
            metadata={"frontmatter": frontmatter} if frontmatter else {},
        )


class TextLoader(DocumentLoader):
    key = "text"
    media_types = frozenset({"text/plain"})
    extensions = frozenset({".txt", ".text", ".log", ".rst"})

    def load(self, data: bytes, *, filename: str) -> LoadedDocument:
        return LoadedDocument(
            text=sanitise(self._decode(data)),
            media_type="text/plain",
            title=Path(filename).stem or "Untitled",
            metadata={},
        )


class CsvLoader(DocumentLoader):
    """Rows rendered as Markdown tables.

    Rendering rather than dumping matters: the chunker recognises Markdown
    tables and keeps them atomic, so a row group reaches the model with its
    header attached. Raw CSV would chunk as prose and lose the columns.
    """

    key = "csv"
    media_types = frozenset({"text/csv", "text/tab-separated-values"})
    extensions = frozenset({".csv", ".tsv"})
    #: Rows per table block. Small enough that a chunk stays useful, large
    #: enough that a spreadsheet does not explode into hundreds of chunks.
    ROWS_PER_BLOCK = 25

    def load(self, data: bytes, *, filename: str) -> LoadedDocument:
        raw = sanitise(self._decode(data))
        delimiter = "\t" if filename.lower().endswith(".tsv") else ","
        try:
            rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
        except csv.Error as exc:
            raise ValidationError(f"Could not parse CSV: {exc}") from exc

        rows = [r for r in rows if any(cell.strip() for cell in r)]
        if not rows:
            return LoadedDocument("", "text/csv", Path(filename).stem, {})

        header, body = rows[0], rows[1:]
        width = len(header)
        parts = [f"# {Path(filename).stem}", ""]

        for start in range(0, max(1, len(body)), self.ROWS_PER_BLOCK):
            block = body[start : start + self.ROWS_PER_BLOCK]
            parts.append(f"| {' | '.join(_cell(c) for c in header)} |")
            parts.append("|" + "---|" * width)
            for row in block:
                padded = (row + [""] * width)[:width]
                parts.append(f"| {' | '.join(_cell(c) for c in padded)} |")
            parts.append("")

        return LoadedDocument(
            text="\n".join(parts),
            media_type="text/csv",
            title=Path(filename).stem or "Table",
            metadata={"columns": header, "row_count": len(body)},
        )


class PdfLoader(DocumentLoader):
    key = "pdf"
    media_types = frozenset({"application/pdf"})
    extensions = frozenset({".pdf"})

    @classmethod
    def available(cls) -> bool:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            return False
        return True

    def load(self, data: bytes, *, filename: str) -> LoadedDocument:
        import pypdf

        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
        except Exception as exc:
            raise ValidationError(
                f"Could not read PDF: {exc}",
                user_message="That PDF could not be read — it may be corrupt.",
            ) from exc

        if reader.is_encrypted:
            # An empty-password decrypt covers PDFs that are "encrypted" only
            # to set permissions, which is common. A real password is refused
            # rather than guessed at.
            try:
                if reader.decrypt("") == 0:
                    raise ValidationError(
                        "PDF is password protected",
                        user_message="That PDF is password protected.",
                    )
            except ValidationError:
                raise
            except Exception as exc:
                raise ValidationError(
                    f"Encrypted PDF could not be opened: {exc}",
                    user_message="That PDF is encrypted and could not be opened.",
                ) from exc

        pages: list[str] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                extracted = page.extract_text() or ""
            except Exception as exc:
                # One unreadable page must not lose the other two hundred.
                log.warning("pdf_page_failed", page=number, error=str(exc))
                continue
            if extracted.strip():
                # Page markers double as provenance: "where did you get this?"
                # can answer with a page number.
                pages.append(f"## Page {number}\n\n{sanitise(extracted).strip()}")

        info = reader.metadata or {}
        title = (
            str(info.get("/Title") or "").strip() or Path(filename).stem or "PDF"
        )
        return LoadedDocument(
            text="\n\n".join(pages),
            media_type="application/pdf",
            title=title,
            metadata={
                "page_count": len(reader.pages),
                "pages_with_text": len(pages),
                "author": str(info.get("/Author") or "") or None,
            },
        )


_LOADERS: list[type[DocumentLoader]] = [
    MarkdownLoader,
    TextLoader,
    CsvLoader,
    PdfLoader,
]

#: Formats §23 mentions that are deliberately absent, with the reason. Surfaced
#: by the API so the UI can say "not supported" rather than silently omitting.
UNSUPPORTED_FORMATS: dict[str, str] = {
    ".docx": "Needs python-docx, which is not installed.",
    ".doc": "Legacy binary Word format; no maintained pure-Python reader.",
    ".html": "Needs an HTML parser; web ingestion arrives with Phase 5.",
    ".epub": "Not required by any current workflow.",
}


def loader_for(filename: str, media_type: str | None = None) -> DocumentLoader:
    suffix = Path(filename).suffix.lower()

    for loader_cls in _LOADERS:
        matches = suffix in loader_cls.extensions or (
            media_type in loader_cls.media_types if media_type else False
        )
        if not matches:
            continue
        if not loader_cls.available():
            raise ValidationError(
                f"Loader '{loader_cls.key}' is unavailable",
                user_message=(
                    f"{suffix} files need an optional dependency that is not "
                    "installed."
                ),
            )
        return loader_cls()

    if suffix in UNSUPPORTED_FORMATS:
        raise ValidationError(
            f"Unsupported format {suffix}",
            user_message=f"{suffix} is not supported. {UNSUPPORTED_FORMATS[suffix]}",
        )

    raise ValidationError(
        f"No loader for {suffix or media_type or 'that file'}",
        user_message=(
            f"I cannot read {suffix or 'that file type'}. Supported: "
            + ", ".join(sorted(available_extensions()))
        ),
    )


def available_extensions() -> set[str]:
    out: set[str] = set()
    for loader_cls in _LOADERS:
        if loader_cls.available():
            out |= loader_cls.extensions
    return out


def available_formats() -> list[dict[str, object]]:
    """What the Knowledge UI renders. Reports reality, including the gaps."""
    formats = [
        {
            "key": loader_cls.key,
            "extensions": sorted(loader_cls.extensions),
            "available": loader_cls.available(),
            "reason": None
            if loader_cls.available()
            else "Optional dependency not installed.",
        }
        for loader_cls in _LOADERS
    ]
    formats.extend(
        {
            "key": extension.lstrip("."),
            "extensions": [extension],
            "available": False,
            "reason": reason,
        }
        for extension, reason in UNSUPPORTED_FORMATS.items()
    )
    return formats


# ── helpers ──────────────────────────────────────────────────────────────────

_FRONTMATTER_KV = re.compile(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$")


def _markdown_title_and_frontmatter(
    text: str, filename: str
) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    frontmatter: dict[str, str] = {}

    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() in {"---", "..."}:
                break
            match = _FRONTMATTER_KV.match(line)
            if match:
                frontmatter[match.group(1).lower()] = match.group(2).strip().strip("\"'")

    title = frontmatter.get("title", "")
    if not title:
        for line in lines:
            heading = re.match(r"^#\s+(.*)$", line.strip())
            if heading:
                title = heading.group(1).strip()
                break
    return title or Path(filename).stem or "Untitled", frontmatter


def _cell(value: str) -> str:
    """Escape pipes so one cell cannot forge extra table columns."""
    return value.replace("|", "\\|").replace("\n", " ").strip()
