"""The backend for machines with no controllable display.

Every method raises :class:`BackendUnavailable` carrying the *specific* reason
detection produced — "no DISPLAY, but Xvfb is installed" rather than
"unsupported". §2 says not to implement functions that are unavailable, and
this is what that looks like when the surrounding architecture still has to
exist: the operation is declared, refused, and the refusal explains itself.

A null backend that returned empty screenshots and pretended clicks landed
would satisfy the type checker and be exactly the fake functionality §44.22
forbids.
"""

from __future__ import annotations

from jarvis.computer.backends.base import BackendUnavailable, DesktopBackend
from jarvis.computer.types import ScreenState, WindowInfo


class UnavailableBackend(DesktopBackend):
    key = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _refuse(self, operation: str) -> None:
        raise BackendUnavailable(f"Cannot {operation}: {self.reason}")

    def screen_size(self) -> tuple[int, int]:
        self._refuse("read the screen size")
        raise AssertionError  # unreachable; keeps the return type honest

    def capture(self) -> bytes:
        self._refuse("take a screenshot")
        raise AssertionError

    def cursor_position(self) -> tuple[int, int]:
        self._refuse("read the cursor position")
        raise AssertionError

    def windows(self) -> list[WindowInfo]:
        self._refuse("list windows")
        raise AssertionError

    def active_window(self) -> WindowInfo | None:
        self._refuse("identify the active window")
        raise AssertionError

    def observe(self) -> ScreenState:
        self._refuse("observe the screen")
        raise AssertionError

    def move_mouse(self, x: int, y: int) -> None:
        self._refuse("move the mouse")

    def click(self, x: int, y: int, *, button: int = 1, count: int = 1) -> None:
        self._refuse("click")

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *,
             button: int = 1) -> None:
        self._refuse("drag")

    def scroll(self, direction: str, amount: int, *, x: int | None = None,
               y: int | None = None) -> None:
        self._refuse("scroll")

    def type_text(self, text: str) -> None:
        self._refuse("type")

    def press_key(self, key: str) -> None:
        self._refuse("press a key")

    def hotkey(self, keys: list[str]) -> None:
        self._refuse("send a hotkey")

    def focus_window(self, window_id: str) -> None:
        self._refuse("focus a window")

    def read_clipboard(self) -> str:
        self._refuse("read the clipboard")
        raise AssertionError

    def write_clipboard(self, text: str) -> None:
        self._refuse("write to the clipboard")
