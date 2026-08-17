"""X11 desktop backend — the real one.

Implemented directly against python-xlib rather than through PyAutoGUI, for
the reason the Phase 0 audit gave: PyAutoGUI is effectively unmaintained, and
on Linux it is a thin wrapper over the same Xlib and XTEST calls made here plus
a subprocess call to a screenshot binary that is not installed on this machine.
Going direct removes a dependency, removes the subprocess, and makes the
failure modes legible.

Three X features carry the implementation:

* **XGetImage** for screenshots — no external binary, no temporary file.
* **XTEST** for synthetic input. It injects at the server, so applications
  cannot distinguish it from hardware, which is what makes it work with
  Chromium's event handling.
* **The window tree plus EWMH properties** for structure. With no accessibility
  bus available, this is the structured layer §4 asks to prefer over pixels:
  window identity, title, geometry and stacking are exact, and only *contents*
  need vision.

## Keyboard mapping

X11 has no "type this string" primitive: text becomes keysyms, keysyms become
keycodes, and keycodes are what XTEST sends. Characters absent from the current
layout are mapped by temporarily rebinding a scratch keycode — the standard
trick, and the only way to type a character the keyboard cannot express. The
mapping is restored afterwards, in a ``finally``, because leaving a rebound
keycode behind would corrupt the user's keyboard.
"""

from __future__ import annotations

import io
import threading
import time
from typing import Any

from jarvis.computer.backends.base import BackendError, BackendUnavailable, DesktopBackend
from jarvis.computer.types import WindowInfo
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Delay between synthetic keystrokes. Without it, applications with
#: JavaScript input handlers drop characters — Chromium reliably loses a few
#: from a burst typed with no gap.
_KEY_DELAY = 0.012
_CLICK_DELAY = 0.06
#: How long to wait for a clipboard owner to answer a selection request.
_CLIPBOARD_TIMEOUT = 1.5

#: Names accepted for special keys, mapped to X keysym names.
_KEY_ALIASES: dict[str, str] = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
    "space": "space", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "insert": "Insert", "ctrl": "Control_L", "control": "Control_L",
    "alt": "Alt_L", "shift": "Shift_L", "super": "Super_L", "win": "Super_L",
    "meta": "Super_L", "capslock": "Caps_Lock", "menu": "Menu",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}

#: Characters needing Shift on a standard US layout.
_SHIFTED = set('~!@#$%^&*()_+{}|:"<>?') | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


class X11Backend(DesktopBackend):
    key = "x11"

    def __init__(self, display_name: str) -> None:
        try:
            from Xlib import display as xdisplay
            from Xlib.ext import xtest  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailable(
                "python-xlib is not installed; X11 control is unavailable."
            ) from exc

        try:
            self._display = xdisplay.Display(display_name)
        except Exception as exc:
            raise BackendUnavailable(
                f"Could not connect to display {display_name}: {exc}"
            ) from exc

        self._display_name = display_name
        self._root = self._display.screen().root
        self._has_xtest = self._display.query_extension("XTEST") is not None
        # Xlib connections are not thread-safe, and the API surface is reached
        # from FastAPI's threadpool as well as the agent loop.
        self._lock = threading.RLock()
        #: (thread, stop_event) for the clipboard selection owner, if any.
        self._clipboard_owner: tuple[threading.Thread, threading.Event] | None = None

        from Xlib import X

        self._X = X
        from Xlib.ext import xtest as xtest_mod

        self._xtest = xtest_mod

        self._atoms = {
            name: self._display.intern_atom(name)
            for name in (
                "_NET_WM_NAME", "UTF8_STRING", "_NET_ACTIVE_WINDOW",
                "_NET_WM_PID", "WM_CLASS", "CLIPBOARD", "TARGETS",
                "_JARVIS_CLIPBOARD",
            )
        }

    # ── observation ──────────────────────────────────────────────────────────

    def screen_size(self) -> tuple[int, int]:
        with self._lock:
            geometry = self._root.get_geometry()
            return geometry.width, geometry.height

    def capture(self) -> bytes:
        from PIL import Image

        with self._lock:
            geometry = self._root.get_geometry()
            try:
                raw = self._root.get_image(
                    0, 0, geometry.width, geometry.height, self._X.ZPixmap, 0xFFFFFFFF
                )
            except Exception as exc:
                raise BackendError(f"Screen capture failed: {exc}") from exc

        # X returns BGRX on a 24-bit TrueColor visual; Pillow reads it directly
        # with the right raw mode rather than needing a channel swap.
        image = Image.frombytes(
            "RGB", (geometry.width, geometry.height), raw.data, "raw", "BGRX"
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False, compress_level=1)
        return buffer.getvalue()

    def cursor_position(self) -> tuple[int, int]:
        with self._lock:
            pointer = self._root.query_pointer()
            return pointer.root_x, pointer.root_y

    def windows(self) -> list[WindowInfo]:
        """Viewable windows, outermost first.

        Walks the tree rather than reading ``_NET_CLIENT_LIST`` because that
        property only exists when a window manager is running, and this
        machine has none. The tree is always there.
        """
        with self._lock:
            active_id = self._active_window_id()
            found: list[WindowInfo] = []
            self._walk(self._root, found, active_id, depth=0)
            found.sort(key=lambda w: w.width * w.height, reverse=True)
            return found

    def _walk(
        self, window: Any, out: list[WindowInfo], active_id: int | None, depth: int
    ) -> None:
        if depth > 6 or len(out) > 60:
            return
        try:
            children = window.query_tree().children
        except Exception:
            return

        for child in children:
            try:
                attributes = child.get_attributes()
                geometry = child.get_geometry()
                # Unmapped windows and the tiny utility windows applications
                # keep for IPC are not part of what a user would call "open".
                if (
                    attributes.map_state == self._X.IsViewable
                    and geometry.width >= 64
                    and geometry.height >= 64
                ):
                    out.append(
                        WindowInfo(
                            id=hex(child.id),
                            title=self._window_title(child),
                            width=geometry.width,
                            height=geometry.height,
                            x=geometry.x,
                            y=geometry.y,
                            application=self._window_application(child),
                            pid=self._window_pid(child),
                            active=child.id == active_id,
                        )
                    )
                    # A viewable top-level is not descended into: its children
                    # are its own widgets, not separate windows.
                    continue
            except Exception:
                continue
            self._walk(child, out, active_id, depth + 1)

    def _window_title(self, window: Any) -> str:
        try:
            prop = window.get_full_property(
                self._atoms["_NET_WM_NAME"], self._atoms["UTF8_STRING"]
            )
            if prop and prop.value:
                return bytes(prop.value).decode("utf-8", "replace")
            return window.get_wm_name() or ""
        except Exception:
            return ""

    def _window_application(self, window: Any) -> str | None:
        try:
            wm_class = window.get_wm_class()
            if wm_class:
                return wm_class[-1]
        except Exception:
            pass
        return None

    def _window_pid(self, window: Any) -> int | None:
        try:
            prop = window.get_full_property(self._atoms["_NET_WM_PID"], 0)
            if prop and prop.value:
                return int(prop.value[0])
        except Exception:
            pass
        return None

    def _active_window_id(self) -> int | None:
        try:
            prop = self._root.get_full_property(self._atoms["_NET_ACTIVE_WINDOW"], 0)
            if prop and prop.value:
                return int(prop.value[0])
        except Exception:
            pass
        # No window manager sets _NET_ACTIVE_WINDOW, so fall back to the input
        # focus, which the server always knows.
        try:
            focus = self._display.get_input_focus().focus
            return focus.id if hasattr(focus, "id") else None
        except Exception:
            return None

    def active_window(self) -> WindowInfo | None:
        for window in self.windows():
            if window.active:
                return window
        # With no window manager the focus often sits on the root; the largest
        # viewable window is then the honest answer to "what is on screen".
        windows = self.windows()
        return windows[0] if windows else None

    # ── mouse ────────────────────────────────────────────────────────────────

    def _require_xtest(self) -> None:
        if not self._has_xtest:
            raise BackendUnavailable(
                "The X server has no XTEST extension, so synthetic input is "
                "impossible."
            )

    def move_mouse(self, x: int, y: int) -> None:
        self._require_xtest()
        x, y = self._clamp(x, y)
        with self._lock:
            self._xtest.fake_input(self._display, self._X.MotionNotify, x=x, y=y)
            self._display.sync()

    def click(self, x: int, y: int, *, button: int = 1, count: int = 1) -> None:
        self._require_xtest()
        x, y = self._clamp(x, y)
        with self._lock:
            self._xtest.fake_input(self._display, self._X.MotionNotify, x=x, y=y)
            self._display.sync()
            time.sleep(_CLICK_DELAY)
            for index in range(count):
                self._xtest.fake_input(self._display, self._X.ButtonPress, button)
                self._xtest.fake_input(self._display, self._X.ButtonRelease, button)
                self._display.sync()
                if index + 1 < count:
                    # Inside the double-click threshold, or the application
                    # sees two single clicks.
                    time.sleep(0.05)
        time.sleep(_CLICK_DELAY)

    def drag(
        self, from_x: int, from_y: int, to_x: int, to_y: int, *, button: int = 1
    ) -> None:
        self._require_xtest()
        from_x, from_y = self._clamp(from_x, from_y)
        to_x, to_y = self._clamp(to_x, to_y)
        with self._lock:
            self._xtest.fake_input(self._display, self._X.MotionNotify, x=from_x, y=from_y)
            self._display.sync()
            time.sleep(_CLICK_DELAY)
            self._xtest.fake_input(self._display, self._X.ButtonPress, button)
            self._display.sync()
            # Interpolated rather than teleported: drag handlers commonly
            # listen for motion events and ignore an instant jump.
            steps = 12
            for step in range(1, steps + 1):
                self._xtest.fake_input(
                    self._display, self._X.MotionNotify,
                    x=int(from_x + (to_x - from_x) * step / steps),
                    y=int(from_y + (to_y - from_y) * step / steps),
                )
                self._display.sync()
                time.sleep(0.012)
            self._xtest.fake_input(self._display, self._X.ButtonRelease, button)
            self._display.sync()
        time.sleep(_CLICK_DELAY)

    def scroll(
        self, direction: str, amount: int, *, x: int | None = None, y: int | None = None
    ) -> None:
        self._require_xtest()
        buttons = {"up": 4, "down": 5, "left": 6, "right": 7}
        button = buttons.get(direction.lower())
        if button is None:
            raise BackendError(f"Unknown scroll direction {direction!r}")

        with self._lock:
            if x is not None and y is not None:
                cx, cy = self._clamp(x, y)
                self._xtest.fake_input(self._display, self._X.MotionNotify, x=cx, y=cy)
                self._display.sync()
            for _ in range(max(1, min(amount, 30))):
                self._xtest.fake_input(self._display, self._X.ButtonPress, button)
                self._xtest.fake_input(self._display, self._X.ButtonRelease, button)
                self._display.sync()
                time.sleep(0.02)

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """Keep the pointer on screen.

        Off-screen coordinates are silently clamped by the server, which makes
        a wrong click land somewhere unexpected instead of failing. Clamping
        here keeps the audit log honest about where the pointer actually went.
        """
        width, height = self.screen_size()
        return max(0, min(int(x), width - 1)), max(0, min(int(y), height - 1))

    # ── keyboard ─────────────────────────────────────────────────────────────

    def type_text(self, text: str) -> None:
        self._require_xtest()
        if len(text) > 10_000:
            raise BackendError("Refusing to type more than 10,000 characters.")
        for char in text:
            self._type_char(char)

    def _type_char(self, char: str) -> None:
        from Xlib import XK

        if char == "\n":
            self.press_key("Return")
            return
        if char == "\t":
            self.press_key("Tab")
            return

        keysym = XK.string_to_keysym(char)
        if keysym == 0:
            keysym = ord(char)

        with self._lock:
            keycode = self._display.keysym_to_keycode(keysym)
            remapped = False
            if keycode == 0:
                keycode = self._borrow_keycode(keysym)
                remapped = True
                if keycode == 0:
                    log.warning("x11_char_unmappable", char=repr(char))
                    return
            try:
                shift = char in _SHIFTED
                shift_code = self._display.keysym_to_keycode(XK.XK_Shift_L)
                if shift and not remapped:
                    self._xtest.fake_input(self._display, self._X.KeyPress, shift_code)
                self._xtest.fake_input(self._display, self._X.KeyPress, keycode)
                self._xtest.fake_input(self._display, self._X.KeyRelease, keycode)
                if shift and not remapped:
                    self._xtest.fake_input(self._display, self._X.KeyRelease, shift_code)
                self._display.sync()
            finally:
                if remapped:
                    # Always restore. A leaked rebinding corrupts the layout
                    # for every application on the display.
                    self._release_keycode(keycode)
        time.sleep(_KEY_DELAY)

    def _borrow_keycode(self, keysym: int) -> int:
        """Temporarily bind a keysym the layout does not provide."""
        try:
            minimum = self._display.display.info.min_keycode
            maximum = self._display.display.info.max_keycode
            mapping = self._display.get_keyboard_mapping(minimum, maximum - minimum + 1)
            for offset, entry in enumerate(mapping):
                if not any(entry):
                    keycode = minimum + offset
                    self._display.change_keyboard_mapping(keycode, [[keysym] * len(entry)])
                    self._display.sync()
                    return keycode
        except Exception as exc:
            log.warning("x11_keycode_borrow_failed", error=str(exc))
        return 0

    def _release_keycode(self, keycode: int) -> None:
        try:
            width = len(self._display.get_keyboard_mapping(keycode, 1)[0])
            self._display.change_keyboard_mapping(keycode, [[0] * width])
            self._display.sync()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("x11_keycode_release_failed", error=str(exc))

    def press_key(self, key: str) -> None:
        self._require_xtest()
        keycode = self._resolve_key(key)
        with self._lock:
            self._xtest.fake_input(self._display, self._X.KeyPress, keycode)
            self._xtest.fake_input(self._display, self._X.KeyRelease, keycode)
            self._display.sync()
        time.sleep(_KEY_DELAY)

    def hotkey(self, keys: list[str]) -> None:
        self._require_xtest()
        if not keys:
            raise BackendError("No keys given.")
        codes = [self._resolve_key(k) for k in keys]
        with self._lock:
            # Modifiers down in order, the final key, then everything up in
            # reverse — the order a real keyboard produces.
            for code in codes:
                self._xtest.fake_input(self._display, self._X.KeyPress, code)
                self._display.sync()
                time.sleep(0.01)
            for code in reversed(codes):
                self._xtest.fake_input(self._display, self._X.KeyRelease, code)
                self._display.sync()
                time.sleep(0.01)

    def _resolve_key(self, key: str) -> int:
        from Xlib import XK

        name = _KEY_ALIASES.get(key.strip().lower(), key.strip())
        keysym = XK.string_to_keysym(name)
        if keysym == 0 and len(name) == 1:
            keysym = ord(name)
        if keysym == 0:
            raise BackendError(f"Unknown key {key!r}")
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            raise BackendError(f"Key {key!r} is not on the current layout")
        return keycode

    # ── windows ──────────────────────────────────────────────────────────────

    def focus_window(self, window_id: str) -> None:
        with self._lock:
            try:
                wid = int(window_id, 16) if window_id.startswith("0x") else int(window_id)
                window = self._display.create_resource_object("window", wid)
                window.configure(stack_mode=self._X.Above)
                window.set_input_focus(self._X.RevertToParent, self._X.CurrentTime)
                self._display.sync()
            except Exception as exc:
                raise BackendError(f"Could not focus window {window_id}: {exc}") from exc

    # ── clipboard ────────────────────────────────────────────────────────────

    def read_clipboard(self) -> str:
        """Request the CLIPBOARD selection from whichever client owns it.

        Uses a **dedicated connection**, not the shared one. X11 delivers
        selection replies as events on the requesting connection, and the
        shared connection is also where the clipboard owner thread waits for
        requests — sharing it means the two consume each other's events and
        both silently return nothing.

        Empty is the honest answer when no client owns the selection, which is
        the normal state on a bare display and is not an error.
        """
        from Xlib import X, Xatom, display as xdisplay

        try:
            conn = xdisplay.Display(self._display_name)
        except Exception as exc:
            raise BackendError(f"Could not open a clipboard connection: {exc}") from exc

        try:
            clipboard = conn.intern_atom("CLIPBOARD")
            utf8 = conn.intern_atom("UTF8_STRING")
            target_property = conn.intern_atom("_JARVIS_CLIPBOARD_IN")

            if conn.get_selection_owner(clipboard) in (X.NONE, 0):
                return ""

            window = conn.screen().root.create_window(
                0, 0, 1, 1, 0, conn.screen().root_depth, window_class=X.InputOutput
            )
            window.change_property(target_property, Xatom.STRING, 8, b"")
            window.convert_selection(clipboard, utf8, target_property, X.CurrentTime)
            conn.flush()

            deadline = time.monotonic() + _CLIPBOARD_TIMEOUT
            while time.monotonic() < deadline:
                if conn.pending_events():
                    event = conn.next_event()
                    if event.type != X.SelectionNotify:
                        continue
                    if event.property in (X.NONE, 0):
                        return ""
                    prop = window.get_full_property(event.property, X.AnyPropertyType)
                    if not (prop and prop.value):
                        return ""
                    value = prop.value
                    return (
                        bytes(value).decode("utf-8", "replace")
                        if isinstance(value, (bytes, bytearray))
                        else str(value)
                    )
                time.sleep(0.01)
            log.warning("clipboard_read_timeout")
            return ""
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def write_clipboard(self, text: str) -> None:
        """Own the CLIPBOARD selection and serve it to anyone who asks.

        X11 clipboards are owner-served: the value lives in the owning client
        and is handed over on request, so "writing" means holding ownership
        for as long as the value should stay available. A dedicated connection
        and a daemon thread do that; the thread exits when another client
        takes ownership, which is exactly when the value stops being ours.

        Replacing a value JARVIS already owns tears down the previous owner
        first, so ownership never leaks a thread per write.
        """
        from Xlib import X, Xatom, display as xdisplay
        from Xlib.protocol import event as xevent

        payload = text.encode("utf-8")
        self._release_clipboard_owner()

        try:
            conn = xdisplay.Display(self._display_name)
        except Exception as exc:
            raise BackendError(f"Could not open a clipboard connection: {exc}") from exc

        clipboard = conn.intern_atom("CLIPBOARD")
        utf8 = conn.intern_atom("UTF8_STRING")
        targets = conn.intern_atom("TARGETS")

        window = conn.screen().root.create_window(
            0, 0, 1, 1, 0, conn.screen().root_depth, window_class=X.InputOutput
        )
        window.set_selection_owner(clipboard, X.CurrentTime)
        conn.flush()
        if conn.get_selection_owner(clipboard) != window:
            conn.close()
            raise BackendError("Could not take ownership of the clipboard.")

        stop = threading.Event()

        def serve() -> None:
            try:
                while not stop.is_set():
                    if not conn.pending_events():
                        time.sleep(0.02)
                        continue
                    ev = conn.next_event()
                    if ev.type == X.SelectionClear:
                        return
                    if ev.type != X.SelectionRequest:
                        continue
                    requestor = ev.requestor
                    prop = ev.property if ev.property not in (X.NONE, 0) else ev.target
                    if ev.target == targets:
                        requestor.change_property(
                            prop, Xatom.ATOM, 32, [targets, utf8, Xatom.STRING]
                        )
                    elif ev.target in (utf8, Xatom.STRING):
                        requestor.change_property(prop, ev.target, 8, payload)
                    else:
                        prop = X.NONE
                    requestor.send_event(
                        xevent.SelectionNotify(
                            time=ev.time, requestor=ev.requestor,
                            selection=ev.selection, target=ev.target, property=prop,
                        ),
                        event_mask=0,
                    )
                    conn.flush()
            except Exception as exc:  # pragma: no cover - thread teardown
                log.debug("clipboard_owner_thread_exit", error=str(exc))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        thread = threading.Thread(target=serve, daemon=True, name="jarvis-clipboard")
        self._clipboard_owner = (thread, stop)
        thread.start()

    def _release_clipboard_owner(self) -> None:
        owner = getattr(self, "_clipboard_owner", None)
        if not owner:
            return
        thread, stop = owner
        stop.set()
        thread.join(timeout=1.0)
        self._clipboard_owner = None

    def close(self) -> None:
        self._release_clipboard_owner()
        try:
            self._display.close()
        except Exception:
            pass
