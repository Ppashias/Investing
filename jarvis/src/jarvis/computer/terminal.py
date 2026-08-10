"""Terminal execution (§18, §28).

Classification lives in :mod:`jarvis.computer.risk`; this is the execution
boundary. Five properties make it defensible, and each closes a specific hole:

1. **No shell.** ``subprocess.exec`` with an argv list, ``shell=False``. There
   is no shell to interpret metacharacters, so injection through a filename or
   an argument cannot reach one. The classifier refuses metacharacters anyway;
   this makes the refusal structural rather than the only line of defence.
2. **Re-classified at execution.** The command is classified again here, from
   the string that is about to run. A caller that classified once and mutated
   the command afterwards gains nothing.
3. **Path arguments inside the allow-list.** A command runs where the
   filesystem guard permits, *and* every argument that names a real location
   is resolved and checked against the same roots. Confining only the working
   directory is not enough: `cat` is a read-only command by any reasonable
   classification, and `cat /home/you/.config/gh/hosts.yml` still hands the
   model a token. §17's boundary has to hold through §18's door or it is not
   a boundary.
4. **A scrubbed environment.** The child gets a minimal, explicitly built
   environment. Inheriting the daemon's would hand every API key in the
   process to any command that runs (§19).
5. **Timeout with process-group kill.** ``start_new_session`` puts the child
   in its own group, so a timeout kills the descendants too rather than
   orphaning them (§28).

Output is truncated and scrubbed before it is returned, because command output
goes into model context and a command can print a credential the environment
scrub did not prevent it from reading.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.computer.risk import CommandAssessment, classify_command, path_is_sensitive
from jarvis.computer.types import ActionRisk
from jarvis.errors import JarvisError
from jarvis.logging import get_logger, scrub_text

log = get_logger(__name__)

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0

#: Environment variables passed to a child process. An allow-list: the daemon
#: holds API keys, and a command that can read them can exfiltrate them.
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "USER", "SHELL")

#: Names that must never be forwarded even if added to the allow-list later.
_ENV_DENY_SUBSTRINGS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH",
    "SESSION", "COOKIE", "PRIVATE",
)


class CommandRefused(JarvisError):
    code = "command_refused"
    http_status = 403
    retryable = False

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message, user_message=user_message or message)


@dataclass(slots=True)
class CommandResult:
    command: str
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    truncated: bool = False
    working_directory: str = ""
    risk: ActionRisk = ActionRisk.HIGH
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": round(self.duration_ms, 1),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "working_directory": self.working_directory,
            "risk": self.risk.value,
            "ok": self.ok,
            "notes": self.notes,
        }

    def summarise(self) -> str:
        """What the model sees. Exit status first — it is the thing that
        determines what to do next, and burying it under output invites the
        model to guess from the text."""
        header = (
            f"$ {self.command}\n"
            f"exit={self.exit_code}"
            + (" (timed out)" if self.timed_out else "")
        )
        parts = [header]
        if self.stdout.strip():
            parts.append(f"stdout:\n{self.stdout.rstrip()}")
        if self.stderr.strip():
            parts.append(f"stderr:\n{self.stderr.rstrip()}")
        if self.truncated:
            parts.append("[output truncated]")
        return "\n\n".join(parts)


def build_environment() -> dict[str, str]:
    """A minimal environment for a child process."""
    env: dict[str, str] = {}
    for name in _ENV_ALLOW:
        value = os.environ.get(name)
        if value is None:
            continue
        if any(marker in name.upper() for marker in _ENV_DENY_SUBSTRINGS):
            continue
        env[name] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Marks the process as JARVIS-launched, so a tool that cares can tell.
    env["JARVIS_MANAGED"] = "1"
    return env


class TerminalExecutor:
    def __init__(
        self,
        *,
        working_directory: Path,
        allowed_roots: list[Path] | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.working_directory = Path(working_directory).expanduser().resolve()
        self.allowed_roots = [Path(r).expanduser().resolve() for r in (allowed_roots or [])]
        self.default_timeout = default_timeout

    def preflight(self, command: str) -> CommandAssessment:
        """Classify without running. Used by the policy engine and the UI."""
        return classify_command(command)

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        working_directory: str | Path | None = None,
    ) -> CommandResult:
        # Re-classified here, from the string about to run, so a caller that
        # classified an earlier version of the command gains nothing.
        assessment = classify_command(command)
        if assessment.risk is ActionRisk.PROHIBITED or not assessment.argv:
            raise CommandRefused(
                f"Refused command: {assessment.reason}", assessment.reason
            )

        cwd = self._resolve_cwd(working_directory)
        self._check_path_arguments(assessment.argv, cwd)
        limit = min(float(timeout or self.default_timeout), MAX_TIMEOUT)
        started = time.perf_counter()

        try:
            process = await asyncio.create_subprocess_exec(
                *assessment.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(cwd),
                env=build_environment(),
                # Own process group, so a timeout kills the whole tree rather
                # than leaving orphans behind holding the terminal open.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CommandRefused(
                f"{assessment.argv[0]} not found",
                f"There is no command called {assessment.argv[0]!r}.",
            ) from exc
        except PermissionError as exc:
            raise CommandRefused(
                f"{assessment.argv[0]} is not executable",
                f"{assessment.argv[0]!r} cannot be executed.",
            ) from exc

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            stdout, stderr = await self._terminate(process)

        duration = (time.perf_counter() - started) * 1000.0
        out, out_truncated = _prepare_output(stdout)
        err, err_truncated = _prepare_output(stderr)

        result = CommandResult(
            command=command,
            argv=assessment.argv,
            exit_code=process.returncode,
            stdout=out,
            stderr=err,
            duration_ms=duration,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
            working_directory=str(cwd),
            risk=assessment.risk,
        )
        if timed_out:
            result.notes.append(f"Killed after {limit:.0f}s.")

        log.info(
            "computer_command_executed",
            program=assessment.argv[0],
            exit_code=process.returncode,
            risk=assessment.risk.value,
            duration_ms=round(duration, 1),
            timed_out=timed_out,
        )
        return result

    async def _terminate(self, process: Any) -> tuple[bytes, bytes]:
        """Kill the process group: TERM, then KILL if it ignores that."""
        for send, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(os.getpgid(process.pid), send)
            except (ProcessLookupError, PermissionError):
                break
            try:
                return await asyncio.wait_for(process.communicate(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        return b"", b""

    def _resolve_cwd(self, requested: str | Path | None) -> Path:
        if requested is None:
            return self.working_directory

        candidate = Path(str(requested)).expanduser().resolve()
        roots = self.allowed_roots or [self.working_directory]
        for root in roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_dir():
                return candidate
            raise CommandRefused(
                f"{candidate} is not a directory",
                f"{candidate} is not a directory.",
            )

        raise CommandRefused(
            f"{candidate} is outside the allowed roots",
            "That working directory is outside the approved directories.",
        )

    def _check_path_arguments(self, argv: list[str], cwd: Path) -> None:
        """Refuse a command whose arguments point outside the allow-list.

        Classification says what a *program* does; it says nothing about what
        it is pointed at. ``cat`` is read-only and therefore LOW, which in
        ASSISTED or AUTONOMOUS mode can run without asking — so without this,
        LOW-risk read commands would be a general-purpose way to read any file
        the daemon can, and the filesystem allow-list would only constrain the
        actions that go through :class:`~jarvis.computer.filesystem.FilesystemGuard`.

        An argument counts as a path when it contains a separator or names
        something that exists relative to the working directory. Everything
        else — flags, grep patterns, git refs — is left alone, because
        treating every token as a path would refuse most real commands.
        Non-existent targets are checked at their nearest existing ancestor,
        so ``touch ../outside/new.txt`` is caught before it creates anything.
        """
        roots = self.allowed_roots or [self.working_directory]

        for arg in argv[1:]:
            if not arg or arg.startswith("-"):
                continue

            candidate = Path(arg)
            looks_like_path = (
                "/" in arg
                or arg in {".", ".."}
                or (cwd / arg).exists()
                or candidate.is_absolute()
            )
            if not looks_like_path:
                continue

            resolved = (
                candidate.expanduser()
                if candidate.is_absolute() or arg.startswith("~")
                else cwd / candidate
            ).resolve(strict=False)

            sensitive = path_is_sensitive(resolved)
            if sensitive:
                raise CommandRefused(
                    f"{resolved} matches {sensitive}",
                    f"{arg} looks like credential storage, so that command "
                    "will not run.",
                )

            # Walk up to the nearest existing ancestor: a path that does not
            # exist yet is still going to be created somewhere, and that
            # somewhere is what the allow-list is about.
            probe = resolved
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent

            if not any(_contains(root, probe) for root in roots):
                raise CommandRefused(
                    f"{resolved} is outside the allowed roots",
                    f"{arg} is outside the approved directories. JARVIS can "
                    "only run commands against "
                    + ", ".join(str(r) for r in roots)
                    + ".",
                )


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare_output(raw: bytes) -> tuple[str, bool]:
    """Decode, scrub, truncate.

    Scrubbed with the same function the log pipeline uses, because command
    output reaches model context: a command that prints a token would
    otherwise put it there, and the environment allow-list cannot prevent a
    command reading a credential from a file it is permitted to read.

    Truncated from the *middle* — the start says what ran and the end says how
    it finished, and both matter more than the middle of a build log.
    """
    text = raw.decode("utf-8", errors="replace") if raw else ""
    text = scrub_text(text)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False

    head = text[: MAX_OUTPUT_CHARS // 2]
    tail = text[-MAX_OUTPUT_CHARS // 2 :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}", True
