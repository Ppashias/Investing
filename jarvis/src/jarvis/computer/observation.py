"""Observation pipeline and screenshot handling (§4, §5, §6, §35).

    COMPUTER → SCREENSHOT / UI STATE → OBSERVATION PROCESSOR
             → STRUCTURED SCREEN STATE → REASONING MODEL → ACTION PLAN

The processor's job is to make observation *cheap*, because §5's four
objectives — latency, tokens, privacy, accuracy — are mostly in tension with
sending a picture. Four mechanisms, in the order they save the most:

1. **Structure before pixels.** Window titles, geometry, focus and cursor come
   from X11 for free. "Which window is focused and how big is it" needs no
   image at all, and with no accessibility bus available (§4) this is the
   structured layer.
2. **Change detection.** A perceptual hash of the frame. An unchanged screen
   is reported as unchanged and the image is not re-sent — the common case
   when polling a long-running task.
3. **Downscaling.** Vision models gain nothing from 1280 physical pixels of a
   text field. Images are scaled to a bounded long edge before they leave.
4. **Cropping to a window.** When the task concerns one window, the rest of
   the screen is neither relevant nor the model's business.

## Retention (§6, §35)

Screenshots are held in memory with a TTL and a hard cap, and are never
written to disk unless the operator turns retention on. A screenshot is the
single most sensitive artifact this system produces: it can contain an open
password manager, a bank balance, or somebody else's message. The default is
therefore *observe when necessary*, not *record everything*.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jarvis.computer.backends.base import DesktopBackend
from jarvis.computer.types import ScreenState, WindowInfo
from jarvis.logging import get_logger

log = get_logger(__name__)

#: Long edge sent to a vision model. 1024 keeps UI text legible while costing
#: roughly a quarter of the tokens of a 2048-wide frame.
DEFAULT_MAX_EDGE = 1024


@dataclass(slots=True)
class StoredScreenshot:
    id: str
    png: bytes
    width: int
    height: int
    captured_at: datetime
    expires_at: float
    signature: list[int]
    #: Set when the operator explicitly asked for this one to be kept.
    persisted_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "width": self.width,
            "height": self.height,
            "bytes": len(self.png),
            "captured_at": self.captured_at.isoformat(),
            "persisted_path": self.persisted_path,
        }


class ScreenshotStore:
    """In-memory, TTL-bounded screenshot storage.

    Memory rather than disk is the deliberate part: a screenshot that was never
    written cannot be recovered from the filesystem later, and the retention
    question answers itself when the process exits. Persisting is possible and
    explicit (§6).
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_items: int = 20,
        persist_dir: Path | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.persist_dir = persist_dir
        self._items: dict[str, StoredScreenshot] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def put(self, png: bytes, *, width: int, height: int) -> StoredScreenshot:
        from jarvis.db.base import new_id

        item = StoredScreenshot(
            id=new_id("shot"),
            png=png,
            width=width,
            height=height,
            captured_at=datetime.now(timezone.utc),
            expires_at=time.monotonic() + self.ttl_seconds,
            signature=tile_signature(png),
        )
        with self._lock:
            self._expire_locked()
            self._items[item.id] = item
            self._order.append(item.id)
            while len(self._order) > self.max_items:
                self._items.pop(self._order.pop(0), None)
        return item

    def get(self, screenshot_id: str) -> StoredScreenshot | None:
        with self._lock:
            self._expire_locked()
            return self._items.get(screenshot_id)

    def latest(self) -> StoredScreenshot | None:
        with self._lock:
            self._expire_locked()
            return self._items.get(self._order[-1]) if self._order else None

    def list(self) -> list[StoredScreenshot]:
        with self._lock:
            self._expire_locked()
            return [self._items[i] for i in reversed(self._order) if i in self._items]

    def persist(self, screenshot_id: str) -> str:
        """Write one screenshot to disk, on explicit request only (§6)."""
        if self.persist_dir is None:
            raise ValueError("Screenshot retention is not configured.")
        item = self.get(screenshot_id)
        if item is None:
            raise KeyError(screenshot_id)

        self.persist_dir.mkdir(parents=True, exist_ok=True)
        path = self.persist_dir / f"{item.id}.png"
        path.write_bytes(item.png)
        item.persisted_path = str(path)
        log.info("screenshot_persisted", screenshot_id=item.id, path=str(path))
        return str(path)

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._order.clear()
        log.info("screenshot_store_cleared", removed=count)
        return count

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for screenshot_id in [
            i for i in self._order if self._items.get(i)
            and self._items[i].expires_at <= now
        ]:
            self._items.pop(screenshot_id, None)
            self._order.remove(screenshot_id)


#: Tiles across and down for the change signature. 32x20 over a 1280x800
#: screen makes each tile 40x40 physical pixels — small enough that text
#: appearing in one input field moves a tile's mean measurably.
_TILE_COLUMNS = 32
_TILE_ROWS = 20


def tile_signature(png: bytes) -> list[int]:
    """Mean brightness per tile — the change signature.

    A global 64-bit dHash is the textbook choice here and it is *wrong for this
    job*: it summarises the whole frame into 64 bits, so typing into one text
    field leaves it identical. That is not a tuning problem, it is the hash
    doing what it is designed to do, and it would make the agent believe its
    own keystrokes had no effect — breaking the verification step (§9) in the
    direction that matters.

    Tiling keeps the comparison local. A change confined to one part of the
    screen moves one or two tiles a long way instead of moving the whole
    signature imperceptibly, and the tiles that moved say *where* it changed.
    """
    from PIL import Image

    image = (
        Image.open(io.BytesIO(png))
        .convert("L")
        .resize((_TILE_COLUMNS, _TILE_ROWS), Image.BOX)  # BOX = mean of each tile
    )
    return list(image.get_flattened_data())


def signature_delta(a: list[int], b: list[int]) -> int:
    """Largest per-tile difference. 0 means identical."""
    if not a or not b or len(a) != len(b):
        return 255
    return max(abs(int(x) - int(y)) for x, y in zip(a, b))


def changed_region(
    a: list[int], b: list[int], width: int, height: int, *, threshold: int = 8
) -> tuple[int, int, int, int] | None:
    """Bounding box of the tiles that moved, in screen coordinates.

    Answers "where did the screen change?", which is what verification wants:
    a click that was supposed to open a dialog and instead changed nothing in
    that area failed, whatever else moved.
    """
    if len(a) != len(b):
        return None
    columns, rows = [], []
    for index, (x, y) in enumerate(zip(a, b)):
        if abs(int(x) - int(y)) >= threshold:
            columns.append(index % _TILE_COLUMNS)
            rows.append(index // _TILE_COLUMNS)
    if not columns:
        return None

    tile_w = width / _TILE_COLUMNS
    tile_h = height / _TILE_ROWS
    return (
        int(min(columns) * tile_w),
        int(min(rows) * tile_h),
        int((max(columns) + 1) * tile_w),
        int((max(rows) + 1) * tile_h),
    )


@dataclass(slots=True)
class ObservationOptions:
    include_image: bool = True
    max_edge: int = DEFAULT_MAX_EDGE
    #: Crop to this window's bounds instead of the whole screen.
    window_id: str | None = None
    #: Largest per-tile brightness delta still counted as "no change".
    #: Measured on this display: an idle screen with a blinking text cursor
    #: sits at 3, and typing a word into a field reaches 7. 4 clears the noise
    #: with margin while still catching the smallest change that matters.
    unchanged_threshold: int = 4
    #: Return the image even when the frame is unchanged. Implied by
    #: ``window_id``: a caller asking for a specific window wants to see it,
    #: not be told it looks the same as the whole screen did last time.
    force_image: bool = False


@dataclass(slots=True)
class Observation:
    state: ScreenState
    #: base64 PNG, downscaled. ``None`` when the frame was unchanged or the
    #: caller did not ask for an image.
    image_base64: str | None = None
    image_width: int = 0
    image_height: int = 0
    scale: float = 1.0
    capture_ms: float = 0.0
    process_ms: float = 0.0
    #: Bounding box of what moved since the previous observation.
    changed_region: tuple[int, int, int, int] | None = None
    #: Largest per-tile brightness delta since the previous observation.
    change_delta: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self, *, include_image: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state.to_dict(),
            "image": {
                "present": self.image_base64 is not None,
                "width": self.image_width,
                "height": self.image_height,
                "scale": round(self.scale, 4),
            },
            "timings": {
                "capture_ms": round(self.capture_ms, 1),
                "process_ms": round(self.process_ms, 1),
            },
            "change": {
                "delta": self.change_delta,
                "region": list(self.changed_region) if self.changed_region else None,
            },
            "notes": self.notes,
        }
        if include_image and self.image_base64:
            payload["image"]["base64"] = self.image_base64
        return payload

    def summarise(self) -> str:
        text = self.state.summarise()
        if self.state.unchanged:
            text += "\nScreen unchanged since the last observation."
        return text


class ObservationProcessor:
    """Turns a backend into structured, budgeted observations."""

    def __init__(
        self,
        backend: DesktopBackend,
        store: ScreenshotStore,
        *,
        default_max_edge: int = DEFAULT_MAX_EDGE,
    ) -> None:
        self.backend = backend
        self.store = store
        self.default_max_edge = default_max_edge
        self._last_signature: list[int] | None = None

    def observe(self, options: ObservationOptions | None = None) -> Observation:
        options = options or ObservationOptions()
        state = self.backend.observe()
        state.captured_at = datetime.now(timezone.utc).isoformat()

        if not state.windows:
            state.notes.append(
                "No application windows are open on this display."
            )

        if not options.include_image:
            return Observation(state=state)

        capture_started = time.perf_counter()
        png = self.backend.capture()
        capture_ms = (time.perf_counter() - capture_started) * 1000.0

        process_started = time.perf_counter()
        stored = self.store.put(png, width=state.width, height=state.height)
        state.screenshot_id = stored.id

        previous = self._last_signature
        delta = signature_delta(previous, stored.signature) if previous else 255
        unchanged = previous is not None and delta <= options.unchanged_threshold
        state.unchanged = unchanged

        region = (
            changed_region(previous, stored.signature, state.width, state.height)
            if previous and not unchanged
            else None
        )
        self._last_signature = stored.signature

        observation = Observation(state=state, capture_ms=capture_ms)
        observation.changed_region = region
        observation.change_delta = delta
        if region:
            state.notes.append(
                f"Screen changed in region x={region[0]}-{region[2]}, "
                f"y={region[1]}-{region[3]}."
            )

        if unchanged and not (options.force_image or options.window_id):
            # The saving that matters: polling a long-running task is the
            # common case, and re-sending an identical frame is pure cost.
            observation.notes.append(
                "Frame is unchanged; image omitted to save tokens. Ask for a "
                "screenshot explicitly if you need to look again."
            )
            observation.process_ms = (time.perf_counter() - process_started) * 1000.0
            return observation

        crop = self._crop_box(state, options.window_id)
        encoded, width, height, scale = self._encode(
            png, max_edge=options.max_edge or self.default_max_edge, crop=crop
        )
        observation.image_base64 = encoded
        observation.image_width = width
        observation.image_height = height
        observation.scale = scale
        if crop:
            observation.notes.append(
                f"Cropped to window bounds {crop}. Coordinates in the image are "
                "relative to that crop; add the window origin before clicking."
            )
        if scale < 1.0:
            observation.notes.append(
                f"Image scaled by {scale:.2f}. Multiply image coordinates by "
                f"{1 / scale:.2f} to get screen coordinates."
            )
        observation.process_ms = (time.perf_counter() - process_started) * 1000.0
        return observation

    def _crop_box(
        self, state: ScreenState, window_id: str | None
    ) -> tuple[int, int, int, int] | None:
        if not window_id:
            return None
        for window in state.windows:
            if window.id == window_id:
                return (
                    max(0, window.x),
                    max(0, window.y),
                    min(state.width, window.x + window.width),
                    min(state.height, window.y + window.height),
                )
        return None

    @staticmethod
    def _encode(
        png: bytes, *, max_edge: int, crop: tuple[int, int, int, int] | None
    ) -> tuple[str, int, int, float]:
        from PIL import Image

        image = Image.open(io.BytesIO(png))
        if crop:
            image = image.crop(crop)

        scale = 1.0
        longest = max(image.width, image.height)
        if longest > max_edge:
            scale = max_edge / longest
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.LANCZOS,
            )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return (
            base64.b64encode(buffer.getvalue()).decode("ascii"),
            image.width,
            image.height,
            scale,
        )


def describe_windows(windows: list[WindowInfo]) -> str:
    if not windows:
        return "No windows."
    return "\n".join(
        f"{w.id} {w.title!r} {w.width}x{w.height}+{w.x}+{w.y}"
        + (" [active]" if w.active else "")
        for w in windows
    )


def content_digest(png: bytes) -> str:
    """Exact digest, for audit records that must identify a specific frame."""
    return hashlib.sha256(png).hexdigest()[:16]
