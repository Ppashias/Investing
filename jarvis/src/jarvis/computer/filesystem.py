"""Filesystem boundaries (§17).

JARVIS gets an allow-list of directories and nothing else. §17 asks for allowed
paths, denied paths, per-operation permissions, and path normalisation against
traversal — all four are here, and the ordering between them is the part that
actually matters.

## The check that works

Three steps, in this order, every time:

1. **Resolve.** ``Path.resolve()`` collapses ``..``, follows symlinks, and
   returns an absolute path. Everything after this point compares real
   locations rather than strings a caller chose.
2. **Deny first.** A denied prefix beats any allow. Otherwise adding a root
   silently re-exposes ``~/.ssh`` underneath it.
3. **Allow by containment**, using ``relative_to`` on resolved paths.

Every one of those steps is load-bearing. String prefixes admit
``/allowed/../etc/shadow``. Unresolved comparison admits a symlink pointing
out of the tree. Allow-before-deny admits credential directories nested inside
a legitimate root. The Phase 2 knowledge ingester learned the first two; this
adds the third, because Phase 3 can *write*.

## Symlinks

Resolution follows them, which is correct for deciding *where a write lands* —
but it means a symlink inside an allowed root pointing at ``/etc`` resolves to
``/etc`` and is then denied. That is the intended outcome. The parent directory
of a new file is resolved too, so creating a file through a symlinked directory
is checked against the real destination.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.computer.risk import path_is_sensitive
from jarvis.errors import JarvisError
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Never reachable, whatever the configuration. These are either system state
#: or credential storage, and no personal-assistant workflow needs them.
DEFAULT_DENIED: tuple[str, ...] = (
    "/etc", "/sys", "/proc", "/dev", "/boot", "/root",
    "/var/log", "/var/lib", "/usr/bin", "/usr/sbin", "/sbin", "/bin",
    "/private/etc", "/System", "/Library/Keychains",
    "C:\\Windows", "C:\\Program Files",
)

#: Denied by name wherever they appear, including inside an allowed root.
DENIED_NAMES: frozenset[str] = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gcloud"}
)

#: Refused regardless of location — a write here changes what runs next login.
DENIED_FILENAMES: frozenset[str] = frozenset(
    {
        ".bashrc", ".bash_profile", ".zshrc", ".profile", ".bash_login",
        "authorized_keys", "known_hosts", "id_rsa", "id_ed25519",
        ".netrc", ".npmrc", ".pypirc", "sudoers", "crontab",
    }
)

#: Files JARVIS will not create or overwrite because doing so causes code to
#: execute later, which turns a file write into arbitrary execution.
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset(
    {".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".exe", ".dll", ".so",
     ".dylib", ".service", ".desktop", ".scpt"}
)

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 8 * 1024 * 1024


class PathNotAllowed(JarvisError):
    code = "path_not_allowed"
    http_status = 403
    retryable = False

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message, user_message=user_message or message)


@dataclass(slots=True)
class FilesystemPolicy:
    allowed_paths: list[Path] = field(default_factory=list)
    denied_paths: list[Path] = field(default_factory=list)
    can_read: bool = True
    can_write: bool = False
    can_delete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_paths": [str(p) for p in self.allowed_paths],
            "denied_paths": [str(p) for p in self.denied_paths],
            "read": self.can_read,
            "write": self.can_write,
            "delete": self.can_delete,
        }


class FilesystemGuard:
    """Resolves and authorises every path before anything touches disk."""

    def __init__(self, policy: FilesystemPolicy) -> None:
        self.policy = policy
        self._allowed = [self._safe_resolve(p) for p in policy.allowed_paths]
        self._denied = [self._safe_resolve(Path(p)) for p in DEFAULT_DENIED]
        self._denied += [self._safe_resolve(p) for p in policy.denied_paths]

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        # strict=False so a path that does not exist yet still normalises —
        # needed for "create this file", where the target is absent by
        # definition but its location must still be checked.
        return Path(os.path.expanduser(str(path))).resolve(strict=False)

    def check(self, raw: str | Path, *, operation: str = "read") -> Path:
        """Authorise a path for an operation. Returns the resolved path."""
        candidate = self._safe_resolve(Path(str(raw)))

        if not self._allowed:
            raise PathNotAllowed(
                "No filesystem roots are configured",
                "No directories are approved for file access. Set "
                "JARVIS_COMPUTER_FILE_ROOTS to allow this.",
            )

        # Deny first — an allowed root must not re-expose a denied subtree.
        for denied in self._denied:
            if _contains(denied, candidate):
                raise PathNotAllowed(
                    f"{candidate} is inside denied path {denied}",
                    f"That path is inside {denied}, which is never accessible.",
                )

        parts_lower = {part.lower() for part in candidate.parts}
        for name in DENIED_NAMES:
            if name.lower() in parts_lower or name.lower() in str(candidate).lower():
                raise PathNotAllowed(
                    f"{candidate} matches denied name {name}",
                    f"Paths containing {name} are never accessible.",
                )

        if candidate.name.lower() in DENIED_FILENAMES:
            raise PathNotAllowed(
                f"{candidate.name} is a protected filename",
                f"{candidate.name} controls what runs on your machine and "
                "cannot be read or written by JARVIS.",
            )

        sensitive = path_is_sensitive(candidate)
        if sensitive:
            raise PathNotAllowed(
                f"{candidate} looks like credential storage ({sensitive})",
                "That path looks like credential storage.",
            )

        if not any(_contains(root, candidate) for root in self._allowed):
            raise PathNotAllowed(
                f"{candidate} is outside every allowed root",
                "That path is outside the approved directories. Approved: "
                + ", ".join(str(r) for r in self._allowed),
            )

        permitted = {
            "read": self.policy.can_read,
            "write": self.policy.can_write,
            "delete": self.policy.can_delete,
        }
        if not permitted.get(operation, False):
            raise PathNotAllowed(
                f"{operation} is not permitted",
                f"File {operation} is not enabled. Turn it on in the Computer "
                "panel if you want JARVIS to do that.",
            )

        if operation in {"write", "delete"} and candidate.suffix.lower() in EXECUTABLE_SUFFIXES:
            raise PathNotAllowed(
                f"{candidate.suffix} is an executable file type",
                f"JARVIS will not {operation} {candidate.suffix} files — "
                "writing one is a way of running code later.",
            )

        return candidate

    # ── operations ───────────────────────────────────────────────────────────

    def read_text(self, path: str | Path) -> tuple[str, dict[str, Any]]:
        resolved = self.check(path, operation="read")
        if not resolved.is_file():
            raise PathNotAllowed(
                f"{resolved} is not a file", f"{resolved.name} is not a readable file."
            )

        size = resolved.stat().st_size
        if size > MAX_READ_BYTES:
            raise PathNotAllowed(
                f"{resolved} is {size} bytes",
                f"{resolved.name} is {size // 1024} KB; the read limit is "
                f"{MAX_READ_BYTES // 1024} KB.",
            )

        data = resolved.read_bytes()
        if b"\x00" in data[:8192]:
            raise PathNotAllowed(
                f"{resolved} appears to be binary",
                f"{resolved.name} looks like a binary file, not text.",
            )

        text = data.decode("utf-8", errors="replace")
        return text, {"path": str(resolved), "bytes": size,
                      "lines": text.count("\n") + 1}

    def write_text(
        self, path: str | Path, content: str, *, overwrite: bool = False
    ) -> dict[str, Any]:
        resolved = self.check(path, operation="write")

        if resolved.exists() and not overwrite:
            raise PathNotAllowed(
                f"{resolved} exists",
                f"{resolved.name} already exists. Pass overwrite to replace it.",
            )
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise PathNotAllowed(
                "Content too large",
                f"That is larger than the {MAX_WRITE_BYTES // 1_048_576} MB "
                "write limit.",
            )
        # The parent is checked separately: a file created through a symlinked
        # directory lands wherever the symlink points, so the destination
        # directory has to clear the allow-list on its own account.
        self.check(resolved.parent, operation="write")
        resolved.parent.mkdir(parents=True, exist_ok=True)

        existed = resolved.exists()
        previous_bytes = resolved.stat().st_size if existed else 0
        resolved.write_text(content, encoding="utf-8")

        log.info(
            "computer_file_written", path=str(resolved), bytes=len(content),
            overwrote=existed,
        )
        return {
            "path": str(resolved),
            "bytes": len(content.encode()),
            "created": not existed,
            "overwrote": existed,
            "previous_bytes": previous_bytes,
        }

    def list_directory(self, path: str | Path, *, limit: int = 200) -> dict[str, Any]:
        resolved = self.check(path, operation="read")
        if not resolved.is_dir():
            raise PathNotAllowed(
                f"{resolved} is not a directory", f"{resolved.name} is not a directory."
            )

        entries: list[dict[str, Any]] = []
        for child in sorted(resolved.iterdir())[:limit]:
            try:
                stat = child.stat()
                entries.append(
                    {
                        "name": child.name,
                        "kind": "directory" if child.is_dir() else "file",
                        "bytes": stat.st_size if child.is_file() else None,
                        # Flagged rather than hidden: the user should know a
                        # denied thing is there, just not be able to read it.
                        "accessible": self._quietly_allowed(child),
                    }
                )
            except OSError:
                continue

        return {"path": str(resolved), "entries": entries,
                "truncated": len(entries) >= limit}

    def create_directory(self, path: str | Path) -> dict[str, Any]:
        resolved = self.check(path, operation="write")
        existed = resolved.exists()
        resolved.mkdir(parents=True, exist_ok=True)
        return {"path": str(resolved), "created": not existed}

    def delete(self, path: str | Path, *, recursive: bool = False) -> dict[str, Any]:
        resolved = self.check(path, operation="delete")
        if not resolved.exists():
            raise PathNotAllowed(
                f"{resolved} does not exist", f"{resolved.name} does not exist."
            )

        # A root itself is never deletable. Removing the allow-listed directory
        # would take the boundary with it.
        for root in self._allowed:
            if resolved == root:
                raise PathNotAllowed(
                    f"{resolved} is an allowed root",
                    "That is one of the approved directories itself, not "
                    "something inside it.",
                )

        if resolved.is_dir():
            if not recursive:
                raise PathNotAllowed(
                    f"{resolved} is a directory",
                    f"{resolved.name} is a directory. Pass recursive to remove it.",
                )
            count = sum(1 for _ in resolved.rglob("*"))
            shutil.rmtree(resolved)
            log.warning("computer_directory_deleted", path=str(resolved), entries=count)
            return {"path": str(resolved), "kind": "directory", "entries_removed": count}

        size = resolved.stat().st_size
        resolved.unlink()
        log.warning("computer_file_deleted", path=str(resolved), bytes=size)
        return {"path": str(resolved), "kind": "file", "bytes": size}

    def move(self, source: str | Path, destination: str | Path) -> dict[str, Any]:
        # Both ends are checked, and the destination as a write: moving a file
        # out of the allow-list would be an exfiltration primitive.
        resolved_source = self.check(source, operation="delete")
        resolved_destination = self.check(destination, operation="write")

        if not resolved_source.exists():
            raise PathNotAllowed(
                f"{resolved_source} does not exist",
                f"{resolved_source.name} does not exist.",
            )
        if resolved_destination.exists():
            raise PathNotAllowed(
                f"{resolved_destination} exists",
                f"{resolved_destination.name} already exists.",
            )

        resolved_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved_source), str(resolved_destination))
        return {"source": str(resolved_source), "destination": str(resolved_destination)}

    def _quietly_allowed(self, path: Path) -> bool:
        try:
            self.check(path, operation="read")
        except PathNotAllowed:
            return False
        return True


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
