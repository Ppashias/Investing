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
    "Windows": ["~/AppData/Roaming/obsidian/obsidian.json"],
}

#: Directories worth a shallow look when the registry is absent. Two levels
#: deep, which covers ``~/Documents/MyVault`` and ``~/Notes`` without walking
#: an entire home directory.
_SCAN_ROOTS = ("~", "~/Documents", "~/Notes", "~/Obsidian", "~/vaults")


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

    @property
    def needs_manual_configuration(self) -> bool:
        return not self.vaults

    def to_dict(self) -> dict[str, Any]:
        return {
            "obsidian_installed": self.obsidian_installed,
            "obsidian_running": self.obsidian_running,
            "registry_found": self.registry_found,
            "vaults": [v.to_dict() for v in self.vaults],
            "needs_manual_configuration": self.needs_manual_configuration,
            "notes": self.notes,
        }


def discover() -> DiscoveryReport:
    """Look for vaults. Never guesses, never writes."""
    report = DiscoveryReport()
    report.obsidian_installed = _obsidian_installed()
    report.obsidian_running = _obsidian_running()

    seen: set[str] = set()
    for vault in _from_registry(report):
        if vault.path not in seen:
            seen.add(vault.path)
            report.vaults.append(vault)

    if not report.vaults:
        for vault in _from_scan():
            if vault.path not in seen:
                seen.add(vault.path)
                report.vaults.append(vault)

    if not report.vaults:
        report.notes.append(
            "No Obsidian vault was found on this machine. A vault is a folder "
            "of Markdown files, so JARVIS needs to be told where one is — set "
            "the vault path in the Obsidian panel or JARVIS_OBSIDIAN_VAULT_PATH."
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


def _from_scan() -> list[DiscoveredVault]:
    """Shallow scan for ``.obsidian/`` directories. Two levels, no deeper."""
    found: list[DiscoveredVault] = []
    for template in _SCAN_ROOTS:
        root = Path(template).expanduser()
        if not root.is_dir():
            continue
        try:
            candidates = [root, *[p for p in root.iterdir() if p.is_dir()]]
        except OSError:
            continue
        for candidate in candidates:
            if candidate.name.startswith("."):
                continue
            if (candidate / ".obsidian").is_dir():
                found.append(
                    DiscoveredVault(
                        name=candidate.name,
                        path=str(candidate),
                        source="scan",
                        has_obsidian_config=True,
                        accessible=os.access(candidate, os.R_OK),
                    )
                )
    return found
