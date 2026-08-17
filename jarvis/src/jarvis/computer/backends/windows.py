"""Windows desktop backend (Phase D, item 9).

**UNVERIFIED — WINDOWS RUNTIME.** Every line here was written and reviewed on
Linux. Nothing in this module has been executed on Windows, no Windows claim in
this docstring is a measurement, and the tests that accompany it exercise the
import guards, the key mapping and the refusal paths — not the input calls,
which cannot run here. That is stated once, plainly, rather than hedged in each
method.

## Why the accessibility tree, and not screenshots

`ONEPUNCHMAN411/Jarvis` reads window contents through UI Automation
(``pywinauto``, ``backend="uia"``) rather than screenshotting and asking a
vision model where things are. That is the same argument this codebase already
won in the browser: an element the system can *name* beats a coordinate it has
to guess, because

* a name survives the window moving, and a coordinate does not;
* "click the *Transfer* button" is a confirmation a person can approve, and
  "click at (840, 312)" is not — §4.3 of the browser control decision, applied
  to the desktop;
* a misresolved name fails, whereas a misresolved coordinate clicks whatever
  happens to be underneath and reports success.

So :meth:`WindowsBackend.elements` is the interesting method, and clicking by
coordinate remains available underneath because some things genuinely have no
accessible element.

## Dependencies are optional, and their absence is a capability answer

``pywinauto`` is not a JARVIS dependency. A machine without it gets
:class:`~jarvis.computer.backends.base.BackendUnavailable` naming the install
command, which is the same shape §3 uses for a missing display — "this platform
cannot do that" is a fact to report, not an error to raise at import time.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any

from jarvis.computer.backends.base import (
    BackendError,
    BackendUnavailable,
    DesktopBackend,
)
from jarvis.computer.types import ScreenState, WindowInfo
from jarvis.logging import get_logger

log = get_logger(__name__)

#: What to tell an operator when a piece is missing. Names the command, because
#: "unavailable" without a remedy is a dead end.
_INSTALL_HINT = (
    "Windows desktop control needs its optional dependencies: "
    "pip install pywinauto pillow"
)

#: Cap on elements returned from one window. A settings dialog has dozens; a
#: browser rendered through UIA has thousands, and a model handed all of them
#: learns less than one handed the first few dozen — and pays for the
#: difference in context it cannot spend on the task. Same reasoning, and
#: deliberately the same number, as browser inspection's MAX_ELEMENTS.
MAX_ELEMENTS = 60

#: Control types worth offering. The things a window is *operated* through.
#: Static text is content and belongs in an observation, not in a list of
#: things to press.
INTERACTIVE_TYPES = frozenset({
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem",
    "MenuItem", "RadioButton", "TabItem", "Tree", "TreeItem",
})

#: JARVIS key names → the ``keyboard`` module's syntax used by pywinauto's
#: ``send_keys``. Explicit rather than pass-through: ``send_keys`` treats
#: ``^%+{}()`` as syntax, so forwarding an arbitrary string would let a key
#: name become a key *sequence*.
_KEYS = {
    "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}",
    "escape": "{ESC}", "space": "{SPACE}", "backspace": "{BACKSPACE}",
    "delete": "{DELETE}", "home": "{HOME}", "end": "{END}",
    "pageup": "{PGUP}", "pagedown": "{PGDN}",
    "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}", "f5": "{F5}",
    "f6": "{F6}", "f7": "{F7}", "f8": "{F8}", "f9": "{F9}", "f10": "{F10}",
    "f11": "{F11}", "f12": "{F12}",
}

#: Modifiers, for :meth:`WindowsBackend.hotkey`.
_MODIFIERS = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+",
              "win": "^{ESC}"}


@dataclass(slots=True)
class UiElement:
    """One named thing in a window.

    ``element_id`` is issued by JARVIS and resolved by lookup, exactly as
    browser element references are: the model can say anything, and saying an
    id nobody issued resolves to nothing.
    """

    element_id: str
    name: str
    control_type: str
    rect: tuple[int, int, int, int]
    enabled: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "name": self.name,
            "control_type": self.control_type,
            "enabled": self.enabled,
        }

    @property
    def centre(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


def _require(module: str) -> Any:
    """Import an optional dependency, or say what is missing and how to fix it."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise BackendUnavailable(f"{_INSTALL_HINT} (missing {module})") from exc


class WindowsBackend(DesktopBackend):
    """Observation and input on Windows, via UI Automation.

    Dumb, like every backend: no permission checks, no risk classification, no
    emergency-stop consultation. Those live in
    :class:`~jarvis.computer.executor.ActionExecutor` above this, and a backend
    that made policy decisions would be a second place for policy to live.
    """

    key = "windows"

    def __init__(self) -> None:
        if platform.system() != "Windows":
            raise BackendUnavailable(
                "The Windows backend only runs on Windows "
                f"(this machine reports {platform.system()})."
            )
        self._desktop: Any = None
        self._elements: dict[str, UiElement] = {}
        #: Bumped whenever the foreground window changes, so element ids issued
        #: against the old one stop resolving. The browser's element registry
        #: learned this the hard way: a locator that outlives its page matches
        #: whatever now sits where the old element was, and acting on it is
        #: reported as a success.
        self._generation = 0
        self._elements_window: str | None = None

    # ── lazily-bound plumbing ────────────────────────────────────────────────

    @property
    def desktop(self) -> Any:
        if self._desktop is None:
            pywinauto = _require("pywinauto")
            self._desktop = pywinauto.Desktop(backend="uia")
        return self._desktop

    @staticmethod
    def _user32() -> Any:
        import ctypes

        return ctypes.windll.user32  # type: ignore[attr-defined]

    # ── observation ──────────────────────────────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        user32 = self._user32()
        # SM_CXSCREEN / SM_CYSCREEN. Primary monitor only: a multi-monitor
        # virtual desktop has negative coordinates on the left-hand screen, and
        # reporting a size that does not match the origin would make every
        # coordinate the model computed wrong in a way nothing would catch.
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))

    def capture(self) -> bytes:
        from io import BytesIO

        image_grab = _require("PIL.ImageGrab")
        try:
            image = image_grab.grab()
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not capture the screen: {exc}") from exc
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def cursor_position(self) -> tuple[int, int]:
        import ctypes

        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        point = _Point()
        self._user32().GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def windows(self) -> list[WindowInfo]:
        found: list[WindowInfo] = []
        try:
            tops = self.desktop.windows()
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not list windows: {exc}") from exc

        for window in tops:
            try:
                if not window.is_visible():
                    continue
                rect = window.rectangle()
                found.append(
                    WindowInfo(
                        window_id=str(window.handle),
                        title=(window.window_text() or "")[:200],
                        application=self._process_name(window),
                        bounds=(rect.left, rect.top,
                                rect.right - rect.left, rect.bottom - rect.top),
                        focused=False,
                    )
                )
            except Exception:  # pragma: no cover - a window that died mid-scan
                continue
        return found

    def active_window(self) -> WindowInfo | None:
        handle = int(self._user32().GetForegroundWindow())
        if not handle:
            return None
        for window in self.windows():
            if window.window_id == str(handle):
                return WindowInfo(
                    window_id=window.window_id,
                    title=window.title,
                    application=window.application,
                    bounds=window.bounds,
                    focused=True,
                )
        return None

    @staticmethod
    def _process_name(window: Any) -> str:
        """Best effort. A window without a readable owner is still a window."""
        try:
            import psutil  # type: ignore[import-not-found]

            return psutil.Process(window.process_id()).name()
        except Exception:
            return ""

    # ── the accessibility tree ───────────────────────────────────────────────

    def elements(self, *, title: str = "", limit: int = MAX_ELEMENTS) -> list[UiElement]:
        """Name what the foreground window can be operated through.

        The method this backend exists for. Structured, bounded, and issuing an
        id per element so a later action can name one — no coordinate crosses
        this boundary unless the caller insists, and then it is their claim to
        justify rather than something the model inferred from a screenshot.
        """
        try:
            window = (
                self.desktop.window(title_re=f".*{title}.*", found_index=0)
                if title
                else self.desktop.window(handle=int(self._user32().GetForegroundWindow()))
            )
            wrapper = window.wrapper_object()
        except Exception as exc:
            raise BackendError(
                f"Could not read that window's contents: {exc}",
                "I could not read what is in that window.",
            ) from exc

        window_key = str(getattr(wrapper, "handle", "")) or (title or "?")
        if window_key != self._elements_window:
            # A different window is a different DOM, in the browser's sense.
            self._generation += 1
            self._elements.clear()
            self._elements_window = window_key

        from jarvis.db.base import new_id

        found: list[UiElement] = []
        try:
            descendants = wrapper.descendants()
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not walk that window: {exc}") from exc

        for control in descendants:
            if len(found) >= limit:
                break
            try:
                control_type = control.element_info.control_type or ""
                if control_type not in INTERACTIVE_TYPES:
                    continue
                name = " ".join((control.window_text() or "").split())[:120]
                rect = control.rectangle()
                element = UiElement(
                    element_id=new_id("ui"),
                    name=name,
                    control_type=control_type,
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    enabled=bool(control.is_enabled()),
                )
            except Exception:  # pragma: no cover - element vanished mid-scan
                continue
            self._elements[element.element_id] = element
            found.append(element)
        return found

    def resolve(self, element_id: str) -> UiElement:
        """An id the model supplied, turned into something real — or refused."""
        element = self._elements.get(element_id)
        if element is None:
            raise BackendError(
                f"There is no element {element_id} on the current window. "
                "It was never issued, or the window has changed since.",
                "That element is not on the window any more — look again.",
            )
        return element

    def click_element(self, element_id: str, *, button: int = 1) -> str:
        """Click a *named* element. Returns what was clicked, for the audit row."""
        element = self.resolve(element_id)
        if not element.enabled:
            raise BackendError(
                f"{element.name or element.control_type} is disabled.",
                f"{element.name or element.control_type} cannot be clicked — "
                "it is greyed out.",
            )
        x, y = element.centre
        self.click(x, y, button=button)
        return f"the {element.control_type} “{element.name}”" if element.name \
            else f"the {element.control_type}"

    # ── mouse ────────────────────────────────────────────────────────────────

    def move_mouse(self, x: int, y: int) -> None:
        self._user32().SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int, *, button: int = 1, count: int = 1) -> None:
        mouse = _require("pywinauto.mouse")
        name = {1: "left", 2: "middle", 3: "right"}.get(button, "left")
        try:
            for _ in range(max(1, count)):
                mouse.click(button=name, coords=(int(x), int(y)))
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not click at ({x}, {y}): {exc}") from exc

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *,
             button: int = 1) -> None:
        mouse = _require("pywinauto.mouse")
        name = {1: "left", 2: "middle", 3: "right"}.get(button, "left")
        try:
            mouse.press(button=name, coords=(int(from_x), int(from_y)))
            mouse.move(coords=(int(to_x), int(to_y)))
            mouse.release(button=name, coords=(int(to_x), int(to_y)))
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not drag: {exc}") from exc

    def scroll(self, direction: str, amount: int, *,
               x: int | None = None, y: int | None = None) -> None:
        mouse = _require("pywinauto.mouse")
        if x is None or y is None:
            x, y = self.cursor_position()
        steps = {"up": 1, "down": -1}.get(direction.lower())
        if steps is None:
            raise BackendError(
                f"'{direction}' is not a scroll direction. Use up or down."
            )
        try:
            mouse.scroll(coords=(int(x), int(y)), wheel_dist=steps * int(amount))
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not scroll: {exc}") from exc

    # ── keyboard ─────────────────────────────────────────────────────────────

    def type_text(self, text: str) -> None:
        """Type literally.

        ``with_spaces``/``with_tabs``/``with_newlines`` are on, and the text is
        escaped, because ``send_keys`` reads ``^%+{}()~`` as syntax. Without
        escaping, typing a password containing ``+`` would press Shift, and
        typing ``{DEL}`` from a document would delete something.
        """
        keyboard = _require("pywinauto.keyboard")
        try:
            keyboard.send_keys(
                self._escape(text),
                with_spaces=True, with_tabs=True, with_newlines=True,
                pause=0.0,
            )
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not type: {exc}") from exc

    @staticmethod
    def _escape(text: str) -> str:
        out: list[str] = []
        for char in text:
            out.append("{" + char + "}" if char in "^%+~(){}[]" else char)
        return "".join(out)

    def press_key(self, key: str) -> None:
        keyboard = _require("pywinauto.keyboard")
        token = _KEYS.get(key.lower().strip())
        if token is None:
            if len(key) == 1:
                token = self._escape(key)
            else:
                raise BackendError(
                    f"'{key}' is not a key I know. Known keys: "
                    + ", ".join(sorted(_KEYS))
                )
        try:
            keyboard.send_keys(token, pause=0.0)
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not press {key}: {exc}") from exc

    def hotkey(self, keys: list[str]) -> None:
        """A combination, assembled from a known vocabulary only."""
        keyboard = _require("pywinauto.keyboard")
        if not keys:
            raise BackendError("No keys were given.")
        prefix = ""
        for modifier in keys[:-1]:
            token = _MODIFIERS.get(modifier.lower().strip())
            if token is None:
                raise BackendError(
                    f"'{modifier}' is not a modifier. Use "
                    + ", ".join(sorted(set(_MODIFIERS) - {"control"}))
                )
            prefix += token
        final = keys[-1].lower().strip()
        token = _KEYS.get(final) or (self._escape(final) if len(final) == 1 else None)
        if token is None:
            raise BackendError(f"'{keys[-1]}' is not a key I know.")
        try:
            keyboard.send_keys(f"{prefix}{token}", pause=0.0)
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not press that combination: {exc}") from exc

    # ── windows ──────────────────────────────────────────────────────────────

    def focus_window(self, window_id: str) -> None:
        try:
            handle = int(window_id)
        except (TypeError, ValueError) as exc:
            raise BackendError(f"'{window_id}' is not a window id.") from exc
        try:
            self.desktop.window(handle=handle).set_focus()
        except Exception as exc:
            raise BackendError(
                f"Could not focus that window: {exc}",
                "I could not bring that window to the front.",
            ) from exc
        # Focus changed, so anything named against the old window is stale.
        self._generation += 1
        self._elements.clear()
        self._elements_window = None

    # ── clipboard ────────────────────────────────────────────────────────────

    def read_clipboard(self) -> str:
        pyperclip = _require("pyperclip")
        try:
            return pyperclip.paste() or ""
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not read the clipboard: {exc}") from exc

    def write_clipboard(self, text: str) -> None:
        pyperclip = _require("pyperclip")
        try:
            pyperclip.copy(text)
        except Exception as exc:  # pragma: no cover - needs a session
            raise BackendError(f"Could not write the clipboard: {exc}") from exc

    def observe(self) -> ScreenState:
        state = super().observe()
        return state

    def close(self) -> None:
        self._desktop = None
        self._elements.clear()
