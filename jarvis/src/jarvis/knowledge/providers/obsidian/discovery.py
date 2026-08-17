"""Finding a vault on this machine (§5).

§5 is explicit about the failure mode to avoid: **do not invent a path.** So
this module only ever *reports* what it found, and the report distinguishes
three states that a naive "vault_path or None" would collapse into one:

* Obsidian's own registry lists vaults — the strongest signal, because the app
  wrote it and it names vaults the user actually opened.
* A directory containing ``.obsidian/`` was found by scanning — a real vault,
  though JARVIS is inferring rather than being told.
* Nothing was found — in which case the answer is a configuration field, not a
  guess.

Scanning is deliberately shallow and bounded. A recursive walk of ``$HOME``
looking for vaults would be slow, would touch every file the daemon can read,
and would be the wrong thing for a knowledge provider to do uninvited.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.logging import get_logger

log = get_logger(__name__)

#: Where the Obsidian desktop app stores its list of known vaults, per OS.
#: Read-only, and only this one file — it is the app's registry, not the vault.
_REGISTRY_PATHS: dict[str, list[str]] = {
    "Linux": [
        "~/.config/obsidian/obsidian.json",
        "~/.var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",
        "~/snap/obsidian/current/.config/obsidian/obsidian.json",
    ],
    "Darwin": ["~/Library/Application Support/obsidian/obsidian.json"],
    "Windows": [
        "~/AppData/Roaming/obsidian/obsidian.json",
        # Roaming can be redirected on managed machines; Local is where the
        # app falls back to.
        "~/AppData/Local/obsidian/obsidian.json",
    ],
}

#: Directories worth a look when the registry is absent.
#:
#: The OneDrive entries are not padding. On Windows, "Documents" is very often
#: redirected to ``%USERPROFILE%\OneDrive\Documents`` — Microsoft turns Known
#: Folder Move on by default for consumer accounts — so a vault sitting in what
#: the user calls Documents is not under ``~/Documents`` at all. A discovery
#: that misses it reports "no vault found" on a machine that has one, which is
#: the most misleading answer available.
_SCAN_ROOTS = (
    "~",
    "~/Documents",
    "~/OneDrive",
    "~/OneDrive/Documents",
    "~/OneDrive - Personal/Documents",
    "~/Notes",
    "~/Obsidian",
    "~/vaults",
    "~/Vaults",
    "~/Dropbox",
    "~/iCloudDrive",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents",
)

#: Windows-only additions, computed at scan time.
#:
#: A vault is not required to live under the home directory, and on Windows it
#: frequently does not — ``C:\Projects\MyVault`` and ``D:\Notes`` are ordinary
#: places to keep one. Every ``_SCAN_ROOTS`` entry is home-relative, so without
#: this a vault outside the user profile is unreachable by scan no matter how
#: deep it goes.
#:
#: Drive roots are cheap to walk given the prune list below: at depth 2 the
#: work is a stat per directory in ``C:\`` and per directory one level under
#: it, with Windows, Program Files and $Recycle.Bin skipped.
def _platform_scan_roots() -> tuple[str, ...]:
    if os.name != "nt":
        return ()

    import string

    roots: list[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if Path(drive).is_dir():
                roots.append(drive)
        except OSError:
            # A mapped drive that is disconnected raises rather than
            # returning False. It is not reachable, so it is not a root.
            continue
    return tuple(roots)


#: How far below each scan root to look. Two levels, because the common layouts
#: are ``~/Documents/MyVault`` (one) and ``~/Documents/Obsidian/MyVault`` (two)
#: — people group their vaults in a folder. Three would start walking source
#: trees and node_modules for no gain.
_SCAN_DEPTH = 2

#: Never descended into. Cheap insurance against a scan root that happens to
#: contain a large tree.
_SKIP_DIRS = frozenset(
    {"node_modules", ".git", "AppData", "Library", "Applications",
     "Windows", "Program Files", "Program Files (x86)", "$Recycle.Bin",
     "venv", ".venv", "__pycache__", "site-packages"}
)


@dataclass(slots=True)
class DiscoveredVault:
    name: str
    path: str
    #: ``registry`` (Obsidian told us) or ``scan`` (we found ``.obsidian/``).
    source: str
    has_obsidian_config: bool
    accessible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "source": self.source,
            "has_obsidian_config": self.has_obsidian_config,
            "accessible": self.accessible,
        }


@dataclass(slots=True)
class DiscoveryReport:
    """What was actually found. Every field is observed, none is assumed."""

    obsidian_installed: bool = False
    obsidian_running: bool = False
    registry_found: bool = False
    vaults: list[DiscoveredVault] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Echoed back when the caller asked for a specific vault, so an empty
    #: result reads as "that one is not here" rather than "there are none".
    requested_name: str | None = None
    #: Where the scan looked. Reported so "not found" is a checkable claim
    #: instead of an assertion — the user can see whether their vault's
    #: location was even considered.
    searched: list[str] = field(default_factory=list)

    @property
    def needs_manual_configuration(self) -> bool:
        return not self.vaults

    def to_dict(self) -> dict[str, Any]:
        return {
            "obsidian_installed": self.obsidian_installed,
            "obsidian_running": self.obsidian_running,
            "registry_found": self.registry_found,
            "requested_name": self.requested_name,
            "vaults": [v.to_dict() for v in self.vaults],
            "needs_manual_configuration": self.needs_manual_configuration,
            "searched": self.searched,
            "notes": self.notes,
        }


def discover(*, name: str | None = None) -> DiscoveryReport:
    """Look for vaults. Never guesses, never writes.

    ``name`` filters to a vault with that name, case-insensitively — a vault's
    name in Obsidian is its folder's basename, so "find the vault called
    Jarvis" is answerable directly rather than by eyeballing a list. The
    registry is consulted first because Obsidian wrote it, then a bounded
    scan; when a name is given the scan runs even if the registry produced
    results, since the named vault may be one the app has never opened.
    """
    report = DiscoveryReport()
    report.obsidian_installed = _obsidian_installed()
    report.obsidian_running = _obsidian_running()
    report.requested_name = name

    wanted = name.strip().lower() if name else None
    seen: set[str] = set()

    def add(vault: DiscoveredVault) -> None:
        if vault.path in seen:
            return
        if wanted and vault.name.lower() != wanted:
            return
        seen.add(vault.path)
        report.vaults.append(vault)

    for vault in _from_registry(report):
        add(vault)

    # A name that the registry did not answer is worth scanning for: a vault
    # created but not yet opened in Obsidian is absent from the registry and
    # present on disk.
    if not report.vaults:
        for vault in _from_scan(report.searched):
            add(vault)

    if not report.vaults:
        report.notes.append(
            (
                f"No Obsidian vault named {name!r} was found on this machine."
                if name
                else "No Obsidian vault was found on this machine."
            )
            + " A vault is a folder of Markdown files, so JARVIS needs to be "
            "told where one is — set the vault path in the Obsidian panel or "
            "JARVIS_OBSIDIAN_VAULT_PATH."
        )
    if not report.obsidian_installed:
        report.notes.append(
            "The Obsidian application is not installed here. That does not "
            "prevent the integration: JARVIS reads the vault files directly, "
            "so it works whether or not the app is present or running."
        )
    return report


# ── internals ────────────────────────────────────────────────────────────────


def _obsidian_installed() -> bool:
    import shutil

    if shutil.which("obsidian"):
        return True
    candidates = [
        "/opt/Obsidian", "/usr/share/obsidian", "/usr/lib/obsidian",
        "/Applications/Obsidian.app",
        "~/Applications/Obsidian.app",
        "~/.local/share/flatpak/app/md.obsidian.Obsidian",
        "/var/lib/flatpak/app/md.obsidian.Obsidian",
    ]
    return any(Path(c).expanduser().exists() for c in candidates)


def _obsidian_running() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            if "obsidian" in name:
                return True
    except Exception:  # pragma: no cover - platform dependent
        return False
    return False


def _from_registry(report: DiscoveryReport) -> list[DiscoveredVault]:
    """Read Obsidian's ``obsidian.json``.

    Its shape is ``{"vaults": {"<id>": {"path": ..., "ts": ..., "open": ...}}}``.
    Anything unexpected is skipped rather than fatal — this is a convenience,
    and a format change in the app must not break the integration.
    """
    found: list[DiscoveredVault] = []
    for template in _REGISTRY_PATHS.get(platform.system(), []):
        path = Path(template).expanduser()
        if not path.is_file():
            continue
        report.registry_found = True
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("obsidian_registry_unreadable", path=str(path), error=str(exc))
            continue

        for entry in (raw.get("vaults") or {}).values():
            if not isinstance(entry, dict):
                continue
            vault_path = entry.get("path")
            if not isinstance(vault_path, str) or not vault_path:
                continue
            resolved = Path(vault_path).expanduser()
            found.append(
                DiscoveredVault(
                    name=resolved.name,
                    path=str(resolved),
                    source="registry",
                    has_obsidian_config=(resolved / ".obsidian").is_dir(),
                    accessible=resolved.is_dir() and os.access(resolved, os.R_OK),
                )
            )
    return found


def _from_scan(searched: list[str] | None = None) -> list[DiscoveredVault]:
    """Bounded scan for ``.obsidian/`` directories.

    Breadth-first to :data:`_SCAN_DEPTH` under each root, pruning the
    directories in :data:`_SKIP_DIRS`. A vault found at depth 2 covers the
    common ``Documents/Obsidian/MyVault`` grouping that a one-level scan
    misses — and missing it reports "no vault found" on a machine that has
    one, which is worse than reporting nothing at all.
    """
    found: list[DiscoveredVault] = []
    seen_roots: set[str] = set()
    for template in (*_SCAN_ROOTS, *_platform_scan_roots()):
        root = Path(template).expanduser()
        if not root.is_dir() or str(root) in seen_roots:
            continue
        seen_roots.add(str(root))
        if searched is not None:
            searched.append(str(root))

        frontier = [(root, 0)]
        while frontier:
            candidate, depth = frontier.pop(0)
            try:
                is_vault = (candidate / ".obsidian").is_dir()
            except OSError:
                continue

            if is_vault:
                found.append(
                    DiscoveredVault(
                        name=candidate.name,
                        path=str(candidate),
                        source="scan",
                        has_obsidian_config=True,
                        accessible=os.access(candidate, os.R_OK),
                    )
                )
                # A vault is not nested inside another vault; stop here.
                continue

            if depth >= _SCAN_DEPTH:
                continue
            try:
                children = sorted(p for p in candidate.iterdir() if p.is_dir())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or child.name in _SKIP_DIRS:
                    continue
                frontier.append((child, depth + 1))
    return found
