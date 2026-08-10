"""Runtime capability detection (§3).

§3 says to inspect the environment rather than assume it, and this module is
that instruction as code: nothing about the platform is hard-coded, everything
is probed at startup, and the result is reported through the API so the UI
renders what is genuinely available.

The rule this enforces is §2's — *do not blindly implement functions that are
unavailable*. An unavailable action is refused with the reason, never silently
skipped and never faked. :meth:`CapabilityReport.reason_unavailable` is what
that refusal quotes.

## Why a virtual display counts as real

On a headless machine there is no screen to observe. JARVIS can create one:
Xvfb provides a genuine X server, and X11 automation against it is the same
code path that would drive a physical desktop. A GUI application launched into
it renders, can be screenshotted, and responds to synthetic input.

That is worth stating precisely, because it would be easy to overclaim. What
works here is **real X11 automation of applications JARVIS itself launched into
a virtual display**. What does not exist on this machine is a physical desktop
with the user's own applications on it. Both facts are reported.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from jarvis.computer.types import ActionKind
from jarvis.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CapabilityReport:
    """What this machine can actually do."""

    os_name: str = ""
    os_release: str = ""
    architecture: str = ""
    python_version: str = ""

    #: A display JARVIS can drive — physical or virtual.
    display: str | None = None
    display_kind: str = "none"        # none | x11 | x11-virtual | wayland
    screen_width: int = 0
    screen_height: int = 0

    has_physical_display: bool = False
    can_create_virtual_display: bool = False
    has_x11_library: bool = False
    has_xtest: bool = False
    has_screenshot: bool = False
    has_pointer_input: bool = False
    has_keyboard_input: bool = False
    has_window_enumeration: bool = False
    has_clipboard: bool = False
    has_accessibility: bool = False
    has_window_manager: bool = False
    has_process_enumeration: bool = False
    has_terminal: bool = False
    has_filesystem: bool = True

    #: Applications JARVIS knows how to launch, name -> executable path.
    known_applications: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def supports(self, kind: ActionKind) -> bool:
        return self.reason_unavailable(kind) is None

    def reason_unavailable(self, kind: ActionKind) -> str | None:
        """Why this action cannot run here, or ``None`` if it can.

        The message is shown to the user and returned to the model, so it says
        what is missing and what would fix it rather than "unsupported".
        """
        display_needed = {
            ActionKind.OBSERVE_SCREEN, ActionKind.SCREENSHOT, ActionKind.GET_CURSOR,
            ActionKind.MOVE_MOUSE, ActionKind.CLICK, ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK, ActionKind.DRAG, ActionKind.SCROLL,
            ActionKind.TYPE_TEXT, ActionKind.PRESS_KEY, ActionKind.HOTKEY,
            ActionKind.GET_WINDOWS, ActionKind.GET_ACTIVE_WINDOW,
            ActionKind.FOCUS_WINDOW, ActionKind.OPEN_APPLICATION,
            ActionKind.CLOSE_APPLICATION, ActionKind.READ_CLIPBOARD,
            ActionKind.WRITE_CLIPBOARD,
        }
        if kind in display_needed and not self.display:
            if self.os_name in {"Windows", "Darwin"}:
                # Not "headless", and emphatically not "install Xvfb". The
                # machine has a screen; JARVIS has no backend for it, and Xvfb
                # would not give it one — X11 automation of an X server does
                # not reach a single Windows or macOS application. Naming a
                # remedy that cannot work is how a user concludes their setup
                # is broken rather than that the feature does not exist.
                return (
                    f"JARVIS has no {self.os_name} computer-control backend. "
                    "Only X11 is implemented, and screen, mouse and keyboard "
                    f"control of a {self.os_name} desktop is not available at "
                    "all — this is a missing feature, not a misconfiguration."
                )
            if self.can_create_virtual_display:
                return (
                    "No display is attached. A virtual display can be started "
                    "(JARVIS_COMPUTER_VIRTUAL_DISPLAY=true) — GUI applications "
                    "JARVIS launches will run inside it."
                )
            return (
                "No display server, and Xvfb is not installed, so there is "
                "nothing to observe or control."
            )

        if kind in {ActionKind.MOVE_MOUSE, ActionKind.CLICK, ActionKind.DOUBLE_CLICK,
                    ActionKind.RIGHT_CLICK, ActionKind.DRAG, ActionKind.SCROLL} \
                and not self.has_pointer_input:
            return "Pointer input needs the X11 XTEST extension, which is absent."

        if kind in {ActionKind.TYPE_TEXT, ActionKind.PRESS_KEY, ActionKind.HOTKEY} \
                and not self.has_keyboard_input:
            return "Keyboard input needs the X11 XTEST extension, which is absent."

        if kind in {ActionKind.READ_CLIPBOARD, ActionKind.WRITE_CLIPBOARD} \
                and not self.has_clipboard:
            return "No clipboard is reachable on this display."

        if kind is ActionKind.EXECUTE_COMMAND and not self.has_terminal:
            return "No shell environment is available."

        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "os": {
                "name": self.os_name,
                "release": self.os_release,
                "architecture": self.architecture,
                "python": self.python_version,
            },
            "display": {
                "value": self.display,
                "kind": self.display_kind,
                "width": self.screen_width,
                "height": self.screen_height,
                "physical": self.has_physical_display,
                "virtual_possible": self.can_create_virtual_display,
                "window_manager": self.has_window_manager,
            },
            "input": {
                "pointer": self.has_pointer_input,
                "keyboard": self.has_keyboard_input,
                "xtest": self.has_xtest,
            },
            "observation": {
                "screenshot": self.has_screenshot,
                "windows": self.has_window_enumeration,
                "accessibility": self.has_accessibility,
            },
            "other": {
                "clipboard": self.has_clipboard,
                "processes": self.has_process_enumeration,
                "terminal": self.has_terminal,
                "filesystem": self.has_filesystem,
            },
            "applications": sorted(self.known_applications),
            "notes": self.notes,
            "actions": {
                kind.value: {
                    "available": self.supports(kind),
                    "reason": self.reason_unavailable(kind),
                }
                for kind in ActionKind
            },
        }


#: Applications JARVIS may launch, mapped to the executables that provide them.
#: An allow-list, not a PATH search: §23 says not to grant automatic access to
#: every application, and the way to honour that is to enumerate the ones that
#: are permitted rather than everything that happens to be installed.
KNOWN_APPLICATIONS: dict[str, list[str]] = {
    "chromium": [
        "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
        "chromium", "chromium-browser", "google-chrome",
    ],
    "text-editor": ["gedit", "kate", "mousepad", "leafpad"],
    "terminal": ["xterm", "gnome-terminal", "konsole"],
    "files": ["nautilus", "dolphin", "thunar"],
    "calculator": ["gnome-calculator", "kcalc", "xcalc"],
}


def detect(*, probe_display: str | None = None) -> CapabilityReport:
    """Probe the environment. Cheap enough to run at startup."""
    report = CapabilityReport(
        os_name=platform.system(),
        os_release=platform.release(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
    )

    try:
        distro = platform.freedesktop_os_release().get("PRETTY_NAME")
        if distro:
            report.notes.append(f"Distribution: {distro}")
    except (OSError, AttributeError):
        pass

    report.has_terminal = bool(shutil.which("sh") or shutil.which("bash"))
    report.can_create_virtual_display = bool(shutil.which("Xvfb"))

    try:
        import psutil  # noqa: F401

        report.has_process_enumeration = True
    except ImportError:
        report.notes.append("psutil is not installed; process listing unavailable.")

    # ── accessibility ────────────────────────────────────────────────────────
    # Checked before the display, because §4 asks that structured
    # accessibility data be preferred over pixels when it exists.
    report.has_accessibility = _detect_accessibility()
    if not report.has_accessibility:
        report.notes.append(
            "No accessibility bus (AT-SPI/dbus). Structured UI element data is "
            "unavailable, so targeting falls back to window geometry plus "
            "visual coordinates."
        )

    # ── display ──────────────────────────────────────────────────────────────
    wayland = os.environ.get("WAYLAND_DISPLAY")
    x_display = probe_display or os.environ.get("DISPLAY")

    if report.os_name in {"Windows", "Darwin"}:
        # Decided before anything is probed, and deliberately regardless of
        # DISPLAY. Running an X server on Windows is ordinary — VcXsrv, X410,
        # WSLg all set DISPLAY — and probing it would succeed: a real X server
        # with XTEST, a width and a height. JARVIS would then report a physical
        # display and working pointer input on a machine where it cannot click
        # a single Windows window. The automation would be real and entirely
        # beside the point.
        #
        # "Headless" would be a lie about a machine with a monitor in front of
        # it. The screen is there; JARVIS has no backend for it. Saying so is
        # the difference between a user thinking their setup is broken and
        # knowing the feature is not built.
        report.notes.append(
            f"{report.os_name} desktop detected. JARVIS has no "
            f"{report.os_name} computer-control backend — only X11 is "
            "implemented — so screen, mouse and keyboard actions are "
            "unavailable here. Everything else, including Obsidian, the "
            "knowledge base and the terminal, works normally."
        )
        if x_display or wayland:
            report.notes.append(
                f"DISPLAY is set ({x_display or wayland}), but an X server on "
                f"{report.os_name} is a separate display containing no "
                f"{report.os_name} applications. Driving it would not touch "
                "the user's desktop, so it is not reported as one."
            )
        return _finalise(report)

    if wayland and not x_display:
        report.display_kind = "wayland"
        report.notes.append(
            "Wayland session detected. JARVIS has no Wayland backend — Wayland "
            "deliberately forbids the screen capture and input injection that "
            "X11 permits, and needs a portal-based implementation."
        )
        return _finalise(report)

    if not x_display:
        report.notes.append(
            "No DISPLAY. This machine is headless."
            + (
                " Xvfb is installed, so JARVIS can create a virtual display and "
                "drive applications it launches into it."
                if report.can_create_virtual_display
                else ""
            )
        )
        return _finalise(report)

    probe = _probe_x11(x_display)
    report.display = x_display if probe["connected"] else None
    report.has_x11_library = probe["library"]
    if not probe["connected"]:
        report.notes.append(probe["error"])
        return _finalise(report)

    report.display_kind = "x11-virtual" if probe["virtual"] else "x11"
    report.has_physical_display = not probe["virtual"]
    report.screen_width = probe["width"]
    report.screen_height = probe["height"]
    report.has_xtest = probe["xtest"]
    report.has_pointer_input = probe["xtest"]
    report.has_keyboard_input = probe["xtest"]
    report.has_screenshot = True
    report.has_window_enumeration = True
    report.has_clipboard = True
    report.has_window_manager = probe["window_manager"]

    if probe["virtual"]:
        report.notes.append(
            f"Display {x_display} is a virtual X server (Xvfb). Automation is "
            "real, but it can only see applications launched into this display "
            "— there is no physical desktop on this machine."
        )
    if not probe["window_manager"]:
        report.notes.append(
            "No window manager is running. Windows have no decorations, and "
            "minimise/maximise are unavailable; raise, focus and geometry work."
        )
    return _finalise(report)


def _finalise(report: CapabilityReport) -> CapabilityReport:
    report.known_applications = _discover_applications()
    log.info(
        "computer_capabilities_detected",
        display=report.display,
        display_kind=report.display_kind,
        pointer=report.has_pointer_input,
        keyboard=report.has_keyboard_input,
        screenshot=report.has_screenshot,
        terminal=report.has_terminal,
        accessibility=report.has_accessibility,
        applications=len(report.known_applications),
    )
    return report


def _detect_accessibility() -> bool:
    if not (
        os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        or os.path.exists("/run/dbus/system_bus_socket")
    ):
        return False
    try:
        import pyatspi  # noqa: F401
    except ImportError:
        return False
    return True


def _probe_x11(display_name: str) -> dict[str, Any]:
    """Connect and ask the server what it supports."""
    result: dict[str, Any] = {
        "connected": False, "library": False, "xtest": False, "virtual": False,
        "width": 0, "height": 0, "window_manager": False, "error": "",
    }
    try:
        from Xlib import display as xdisplay
    except ImportError:
        result["error"] = (
            "python-xlib is not installed; X11 observation and control are "
            "unavailable."
        )
        return result

    result["library"] = True
    try:
        conn = xdisplay.Display(display_name)
    except Exception as exc:
        result["error"] = f"Could not connect to display {display_name}: {exc}"
        return result

    try:
        screen = conn.screen()
        geometry = screen.root.get_geometry()
        result.update(
            connected=True,
            width=geometry.width,
            height=geometry.height,
            xtest=conn.query_extension("XTEST") is not None,
        )
        result["virtual"] = _display_is_virtual(display_name, conn)

        # A window manager claims _NET_SUPPORTING_WM_CHECK. Its absence is the
        # difference between "can minimise a window" and "cannot".
        atom = conn.intern_atom("_NET_SUPPORTING_WM_CHECK")
        prop = screen.root.get_full_property(atom, 0)
        result["window_manager"] = bool(prop and prop.value)
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = f"X11 probe failed: {exc}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


#: RANDR output names that indicate a real connector. Xvfb and the dummy
#: driver expose a single output called ``screen``; physical hardware exposes
#: ``eDP-1``, ``HDMI-2``, ``DP-1`` and similar.
_HARDWARE_OUTPUT_PREFIXES = (
    "edp", "hdmi", "dp-", "displayport", "vga", "lvds", "dvi", "tv-", "none-",
)


def _display_is_virtual(display_name: str, conn: Any) -> bool:
    """Decide whether a display is a virtual X server.

    The vendor string is useless for this — Xvfb reports "The X.Org
    Foundation" exactly as a real server does. Two better signals:

    1. An ``Xvfb`` process whose command line names this display. Exact when
       it holds, which is the case that matters most since JARVIS may have
       started it.
    2. RANDR output names. Real hardware exposes a connector name; Xvfb
       exposes a single output called ``screen``.

    Uncertainty resolves to *virtual*. Claiming a physical desktop that does
    not exist is the worse error: it implies JARVIS can see the user's actual
    applications.
    """
    try:
        import psutil

        for process in psutil.process_iter(["name", "cmdline"]):
            info = process.info
            if (info.get("name") or "").lower().startswith("xvfb"):
                if display_name in (info.get("cmdline") or []):
                    return True
    except Exception:  # pragma: no cover - psutil optional
        pass

    try:
        from Xlib.ext import randr

        resources = randr.get_screen_resources(conn.screen().root)
        names = []
        for output in resources.outputs:
            try:
                names.append(
                    randr.get_output_info(conn.screen().root, output, 0)
                    .name.lower()
                )
            except Exception:
                continue
        if names:
            return not any(
                n.startswith(_HARDWARE_OUTPUT_PREFIXES) for n in names
            )
    except Exception:
        pass

    return True


def _discover_applications() -> dict[str, str]:
    """Resolve the allow-list against what is installed."""
    found: dict[str, str] = {}
    for name, candidates in KNOWN_APPLICATIONS.items():
        for candidate in candidates:
            path = candidate if os.path.isfile(candidate) else shutil.which(candidate)
            if path and os.access(path, os.X_OK):
                found[name] = path
                break
    return found


def start_virtual_display(
    *, width: int = 1280, height: int = 800, number: int = 88
) -> tuple[str, subprocess.Popen[bytes]] | None:
    """Start an Xvfb server and return ``(display, process)``.

    Used when the operator opts in with ``JARVIS_COMPUTER_VIRTUAL_DISPLAY``.
    Bound with ``-nolisten tcp`` so the display is reachable only through the
    local socket — a virtual display accepting network connections would be a
    remote input-injection surface.

    Several display numbers are tried in turn. A display number can be taken by
    a live server or, more annoyingly, by a stale ``/tmp/.X<n>-lock`` left by
    one that was killed — and a daemon that gives up because of a lock file
    from a previous run would look, from the outside, exactly like a machine
    with no display at all.
    """
    if not shutil.which("Xvfb"):
        return None

    import time

    for candidate in range(number, number + 8):
        display_name = f":{candidate}"
        process = subprocess.Popen(
            [
                "Xvfb", display_name, "-screen", "0", f"{width}x{height}x24",
                "-nolisten", "tcp",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        started = False
        for _ in range(50):
            time.sleep(0.1)
            if process.poll() is not None:
                log.info("virtual_display_number_busy", display=display_name)
                break
            if _probe_x11(display_name)["connected"]:
                started = True
                break

        if started:
            log.info("virtual_display_started", display=display_name,
                     size=f"{width}x{height}")
            return display_name, process

        if process.poll() is None:
            process.terminate()
            log.warning("virtual_display_timeout", display=display_name)
            return None

    log.warning("virtual_display_unavailable", tried=f":{number}-:{number + 7}")
    return None
