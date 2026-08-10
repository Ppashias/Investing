"""Desktop backend interface (§2, §33).

The abstraction §2 asks for. Two implementations ship: :mod:`.x11` (real, via
python-xlib and XTEST) and :mod:`.unavailable` (refuses with a reason).

Every method may raise :class:`BackendUnavailable`. That is the honest failure
for "this platform cannot do that", and it is distinct from an action failing:
a click that misses is a failure, a click on a machine with no pointer is
unavailable, and conflating them would make a headless server look like a
broken desktop.

Backends are deliberately **dumb**. They do not check permissions, classify
risk, or consult the emergency stop — those all happen in
:class:`~jarvis.computer.executor.ActionExecutor`, above this layer. A backend
that made policy decisions would be a second place for policy to live, and
§13's "never allow individual tools to bypass this system" would stop being
checkable.
"""

from __future__ import annotations

import abc

from jarvis.computer.types import ScreenState, WindowInfo
from jarvis.errors import JarvisError


class BackendUnavailable(JarvisError):
    """The platform cannot perform this operation."""

    code = "computer_capability_unavailable"
    http_status = 501
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message, user_message=message)


class BackendError(JarvisError):
    """The operation was attempted and failed."""

    code = "computer_action_failed"
    http_status = 500
    retryable = True


class DesktopBackend(abc.ABC):
    """Observation and input for one display."""

    key: str = "base"

    # ── observation ──────────────────────────────────────────────────────────

    @abc.abstractmethod
    def screen_size(self) -> tuple[int, int]: ...

    @abc.abstractmethod
    def capture(self) -> bytes:
        """Full-screen PNG bytes."""

    @abc.abstractmethod
    def cursor_position(self) -> tuple[int, int]: ...

    @abc.abstractmethod
    def windows(self) -> list[WindowInfo]: ...

    @abc.abstractmethod
    def active_window(self) -> WindowInfo | None: ...

    def observe(self) -> ScreenState:
        """Structured state without a screenshot.

        Separate from :meth:`capture` because most questions — which window is
        focused, how big is it, where is the cursor — are answerable from
        metadata that costs microseconds and no tokens (§5).
        """
        width, height = self.screen_size()
        windows = self.windows()
        return ScreenState(
            width=width,
            height=height,
            windows=windows,
            active_window=self.active_window(),
            cursor=self.cursor_position(),
        )

    # ── mouse ────────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def move_mouse(self, x: int, y: int) -> None: ...

    @abc.abstractmethod
    def click(self, x: int, y: int, *, button: int = 1, count: int = 1) -> None: ...

    @abc.abstractmethod
    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *,
             button: int = 1) -> None: ...

    @abc.abstractmethod
    def scroll(self, direction: str, amount: int, *,
               x: int | None = None, y: int | None = None) -> None: ...

    # ── keyboard ─────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def type_text(self, text: str) -> None: ...

    @abc.abstractmethod
    def press_key(self, key: str) -> None: ...

    @abc.abstractmethod
    def hotkey(self, keys: list[str]) -> None: ...

    # ── windows ──────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def focus_window(self, window_id: str) -> None: ...

    # ── clipboard ────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def read_clipboard(self) -> str: ...

    @abc.abstractmethod
    def write_clipboard(self, text: str) -> None: ...

    def close(self) -> None:
        """Release resources. Safe to call more than once."""
