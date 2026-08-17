"""Risk classification (§12) and the command policy (§18).

Risk is **computed from the action's content**, never taken from the caller.
:func:`classify_risk` starts at the action kind's baseline and raises it based
on what the action would actually do — a write inside a scratch directory and a
write to `~/.bashrc` are the same `ActionKind` and are not the same risk.

The direction of travel is one-way: classification can only *raise*. There is
no path by which inspecting an action's contents makes it safer, and allowing
one would turn every heuristic here into an attack surface.

## Command classification

§18 asks for a command policy rather than free shell access. This module
provides the classification; :mod:`jarvis.computer.terminal` provides the
execution boundary. Three rules underpin it:

1. **Allow-list the safe, pattern-match the dangerous, refuse the unknown.**
   An unrecognised command is `HIGH`, not `LOW` — the default for something
   nobody has reasoned about is "ask a human".
2. **Shell metacharacters are themselves a risk.** `git status && rm -rf ~` is
   not a `git status`. Commands are refused rather than parsed heuristically
   when they contain shell control operators, and executed without a shell.
3. **Some things are never allowed.** Disk formatting, fork bombs, and piping
   the internet into a shell are `PROHIBITED`: no mode and no grant enables
   them, because there is no version of this system where an AI agent should
   run them unattended.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from jarvis.computer.types import BASE_RISK, ActionKind, ActionRisk, ComputerAction


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    risk: ActionRisk
    #: Why, in one line — shown in the confirmation dialog and the audit log.
    reason: str
    #: True when the effect cannot be undone by JARVIS. Feeds the permission
    #: engine's irreversibility floor.
    irreversible: bool = False


# ── commands ─────────────────────────────────────────────────────────────────

#: Read-only commands. Named individually: a prefix match would let
#: ``git push`` in behind ``git``.
_LOW_COMMANDS: dict[str, frozenset[str]] = {
    "ls": frozenset(), "pwd": frozenset(), "whoami": frozenset(), "date": frozenset(),
    "echo": frozenset(), "cat": frozenset(), "head": frozenset(), "tail": frozenset(),
    "wc": frozenset(), "file": frozenset(), "stat": frozenset(), "du": frozenset(),
    "df": frozenset(), "uname": frozenset(), "hostname": frozenset(),
    "env": frozenset(), "printenv": frozenset(), "which": frozenset(),
    "tree": frozenset(), "find": frozenset(), "grep": frozenset(), "rg": frozenset(),
    "diff": frozenset(), "sort": frozenset(), "uniq": frozenset(),
    "git": frozenset({"status", "log", "diff", "show", "branch", "remote",
                      "describe", "rev-parse", "ls-files", "blame", "stash"}),
    "python3": frozenset({"--version"}),
    "node": frozenset({"--version"}),
    "npm": frozenset({"--version", "ls", "list", "view", "outdated"}),
    "pip": frozenset({"--version", "list", "show", "freeze"}),
    "pytest": frozenset({"--version", "--collect-only"}),
}

#: Build, test and dependency operations. Real side effects, bounded blast
#: radius, and the everyday content of development work.
_MEDIUM_COMMANDS: dict[str, frozenset[str]] = {
    "mkdir": frozenset(), "touch": frozenset(), "cp": frozenset(),
    "pytest": frozenset(), "make": frozenset(), "tsc": frozenset(),
    "git": frozenset({"add", "commit", "checkout", "switch", "restore", "merge",
                      "rebase", "fetch", "pull", "tag", "init", "clone"}),
    "npm": frozenset({"install", "ci", "run", "test", "build"}),
    "pip": frozenset({"install"}),
    "python3": frozenset(), "node": frozenset(),
}

#: Destructive, privileged, or outward-facing. Always confirmed, never
#: auto-allowed.
_HIGH_COMMANDS: frozenset[str] = frozenset(
    {
        "rm", "rmdir", "mv", "chmod", "chown", "chgrp", "ln", "truncate",
        "kill", "killall", "pkill", "systemctl", "service", "mount", "umount",
        "crontab", "at", "ssh", "scp", "rsync", "sftp", "ftp",
        "curl", "wget", "nc", "ncat", "telnet",
        "docker", "podman", "kubectl", "terraform", "aws", "gcloud", "az",
        "apt", "apt-get", "dpkg", "snap", "brew", "yum", "dnf", "pacman",
        "useradd", "userdel", "usermod", "passwd", "gpg", "openssl", "keytool",
        "iptables", "ufw", "sysctl", "modprobe", "insmod",
    }
)

#: Never permitted. Not "confirm harder" — refused.
_PROHIBITED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("recursive root delete", re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*\s+/\s*$")),
    ("recursive root delete", re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*f?\s+/(\s|$)")),
    ("disk format", re.compile(r"\b(mkfs|mke2fs|fdisk|parted|diskutil|format)\b")),
    ("raw disk write", re.compile(r"\bdd\b[^|;&]*\bof=/dev/")),
    ("fork bomb", re.compile(r":\(\)\s*\{.*\}\s*;?\s*:")),
    ("pipe network to shell", re.compile(
        r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|d)?sh\b")),
    ("privilege escalation", re.compile(r"^\s*(sudo|doas|su)\b")),
    ("history or credential exfiltration", re.compile(
        r"\b(cat|less|more|head|tail|cp|scp)\b[^|;&]*"
        r"(\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.netrc|shadow|\.env\b)")),
    ("shutdown", re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b")),
]

#: Shell control operators. Their presence means the string is a shell program
#: rather than a command, and it is refused rather than analysed — analysing
#: shell grammar to decide whether it is safe is a losing game.
_SHELL_METACHARACTERS = re.compile(r"[;&|<>`$(){}\[\]!*?~\n\\]|\|\||&&")


@dataclass(frozen=True, slots=True)
class CommandAssessment:
    risk: ActionRisk
    reason: str
    #: Parsed argv. ``None`` when the command could not be safely parsed, in
    #: which case it must not be executed.
    argv: list[str] | None = None
    irreversible: bool = False


def classify_command(command: str) -> CommandAssessment:
    """Classify a command string. Never executes anything."""
    text = command.strip()
    if not text:
        return CommandAssessment(ActionRisk.PROHIBITED, "Empty command.")

    for label, pattern in _PROHIBITED_PATTERNS:
        if pattern.search(text):
            return CommandAssessment(
                ActionRisk.PROHIBITED,
                f"Refused: {label}. This is never permitted, in any mode.",
                irreversible=True,
            )

    if _SHELL_METACHARACTERS.search(text):
        return CommandAssessment(
            ActionRisk.PROHIBITED,
            "Refused: contains shell operators. Commands run without a shell, "
            "so pipes, redirects, substitution and chaining are unavailable. "
            "Run one command at a time.",
        )

    try:
        argv = shlex.split(text)
    except ValueError as exc:
        return CommandAssessment(
            ActionRisk.PROHIBITED, f"Refused: could not parse ({exc})."
        )
    if not argv:
        return CommandAssessment(ActionRisk.PROHIBITED, "Refused: empty after parsing.")

    program = Path(argv[0]).name
    # The first argument, flag or not. Flags matter: ``python3 --version`` is
    # read-only while ``python3 script.py`` is not, and treating a leading
    # flag as "no subcommand" loses that distinction in both directions.
    token = argv[1] if len(argv) > 1 else None

    def matches(table: dict[str, frozenset[str]]) -> bool:
        """An empty allow-list means every invocation of the program."""
        if program not in table:
            return False
        allowed = table[program]
        return not allowed or (token is not None and token in allowed)

    if program in _HIGH_COMMANDS:
        return CommandAssessment(
            ActionRisk.HIGH,
            f"{program} can modify or transmit data outside JARVIS.",
            argv=argv,
            irreversible=program in {"rm", "rmdir", "mv", "truncate", "chmod", "chown"},
        )

    if matches(_LOW_COMMANDS):
        return CommandAssessment(ActionRisk.LOW, f"{program} is read-only.", argv=argv)

    if matches(_MEDIUM_COMMANDS):
        return CommandAssessment(
            ActionRisk.MEDIUM,
            f"{program} {token or ''}".strip() + " changes local state.",
            argv=argv,
        )

    if program in _LOW_COMMANDS or program in _MEDIUM_COMMANDS:
        # A known program used in a way the policy does not cover: `git push`
        # is not `git log`. Known-program-unknown-use is still unknown.
        return CommandAssessment(
            ActionRisk.HIGH,
            f"{program} {token or ''}".strip()
            + " is not a recognised operation in the command policy.",
            argv=argv,
        )

    # The important default. Unknown means HIGH, so a command nobody has
    # classified meets a human rather than running.
    return CommandAssessment(
        ActionRisk.HIGH,
        f"{program} is not in the command policy; treating as high risk.",
        argv=argv,
    )


# ── paths ────────────────────────────────────────────────────────────────────

#: Path fragments that raise a filesystem action's risk regardless of where the
#: allow-list sits. Belt and braces: the allow-list should already exclude
#: these, and if a root is ever misconfigured this still catches them.
_SENSITIVE_PATH_PARTS = (
    ".ssh", ".gnupg", ".aws", ".config/gcloud", ".netrc", ".env",
    "id_rsa", "id_ed25519", "credentials", "shadow", "passwd",
    ".git/config", ".npmrc", ".pypirc", ".docker/config.json",
)


def path_is_sensitive(path: str | Path) -> str | None:
    """Return the matching fragment, or ``None``."""
    text = str(path).replace("\\", "/").lower()
    for part in _SENSITIVE_PATH_PARTS:
        if part in text:
            return part
    return None


# ── actions ──────────────────────────────────────────────────────────────────


def classify_risk(action: ComputerAction) -> RiskAssessment:
    """The single risk classifier. Every action goes through it."""
    base = BASE_RISK.get(action.kind, ActionRisk.HIGH)
    reason = f"{action.kind.value} baseline"
    irreversible = False

    if action.kind is ActionKind.EXECUTE_COMMAND:
        # The classifier has read the actual command, so its verdict replaces
        # the baseline rather than being maxed with it. The baseline exists for
        # kinds whose content is never inspected; keeping it here would floor
        # every command at HIGH and make `pwd` indistinguishable from `rm`,
        # which would train the user to approve everything.
        assessment = classify_command(str(action.params.get("command", "")))
        return RiskAssessment(
            risk=assessment.risk,
            reason=assessment.reason,
            irreversible=assessment.irreversible,
        )

    if action.kind in {
        ActionKind.READ_FILE, ActionKind.WRITE_FILE, ActionKind.DELETE_PATH,
        ActionKind.LIST_DIRECTORY, ActionKind.CREATE_DIRECTORY,
    }:
        path = action.params.get("path", "")
        sensitive = path_is_sensitive(path)
        if sensitive:
            return RiskAssessment(
                ActionRisk.PROHIBITED,
                f"Refused: path looks like credential storage ({sensitive}).",
                irreversible=action.kind is ActionKind.DELETE_PATH,
            )
        if action.kind is ActionKind.DELETE_PATH:
            recursive = bool(action.params.get("recursive"))
            return RiskAssessment(
                ActionRisk.HIGH,
                "Deleting a directory tree." if recursive else "Deleting a file.",
                irreversible=True,
            )
        if action.kind is ActionKind.WRITE_FILE:
            return RiskAssessment(
                ActionRisk.MEDIUM,
                "Overwriting an existing file."
                if action.params.get("overwrite")
                else "Writing a file.",
                # An overwrite destroys the previous contents; a create does not.
                irreversible=bool(action.params.get("overwrite")),
            )

    if action.kind is ActionKind.MOVE_PATH:
        for key in ("source", "destination"):
            sensitive = path_is_sensitive(action.params.get(key, ""))
            if sensitive:
                return RiskAssessment(
                    ActionRisk.PROHIBITED,
                    f"Refused: {key} looks like credential storage ({sensitive}).",
                    irreversible=True,
                )
        return RiskAssessment(ActionRisk.MEDIUM, "Moving a path.", irreversible=True)

    if action.kind is ActionKind.TYPE_TEXT:
        # Typing is how a credential reaches an application. The guard already
        # refuses to *store* one; this refuses to type one on the model's say-so.
        from jarvis.memory import guard

        verdict = guard.inspect(str(action.params.get("text", "")))
        if verdict.blocked:
            return RiskAssessment(
                ActionRisk.PROHIBITED,
                f"Refused: the text to type looks like a credential "
                f"({verdict.reason}).",
            )

    if action.kind is ActionKind.WRITE_CLIPBOARD:
        from jarvis.memory import guard

        if guard.inspect(str(action.params.get("text", ""))).blocked:
            return RiskAssessment(
                ActionRisk.HIGH, "Placing credential-shaped text on the clipboard."
            )

    if action.kind is ActionKind.CLOSE_APPLICATION:
        return RiskAssessment(
            ActionRisk.HIGH,
            "Closing an application can discard unsaved work.",
            irreversible=True,
        )

    return RiskAssessment(base, reason, irreversible)


def _max_risk(a: ActionRisk, b: ActionRisk) -> ActionRisk:
    return a if a.rank >= b.rank else b
