"""
gui/avatar.py — animated GIF avatar for Zara.

WHAT THIS REPLACES
------------------
The previous centrepiece was a software-rendered 3-D holographic head
(`core/avatar.py::HoloAvatar`) painted with QPainter inside `ui.py`. This module
replaces that rendering *widget* with a far simpler and far cheaper one: a
pre-decoded, in-memory GIF played back on a Tkinter `after()` loop.

The old head is left untouched in `core/` — nothing here imports or mutates it —
so the PyQt6 HUD keeps working exactly as before. This is an additional,
self-contained avatar surface (chosen deliberately over a rewrite of the 6000
line PyQt UI).

ARCHITECTURE
------------
                backend threads (STT / TTS / Gemini / skills)
                              |
                              |  bus.emit("Speaking")      thread-safe
                              v
                    AvatarEventBus  (queue.Queue)
                              |
                 drain() on the Tk main loop only
                              v
        AvatarView  <-- root.after(~35 ms, self._tick) -->  GIF frame advance
                                                           or pulsing ring

                AvatarController ties the bus, the view and the after() pump
                together and can own a dedicated Tk root in its own thread when
                Zara's main loop is not Tk (which, today, it is not).

DESIGN NOTES
------------
* Frames are decoded and resized **once at load**, then stored as PNG bytes.
  The PIL image object is closed immediately, so a 50-frame GIF costs a few MB
  of bytes rather than an 70 MB+ stack of RGBA buffers.
* The GIF's own per-frame delay is respected, so a 5 s / 50-frame clip loops in
  exactly 5 s. `fps` controls the *scheduler* rate (25–30 Hz as required) so the
  loop is smooth; motion is limited by the source frame rate. Pass
  `interpolate=True` to upsample motion by cross-fading adjacent frames.
* If the GIF is missing or corrupt, the view draws a dynamic pulsing vector
  ring instead. It never raises into the caller.
* Thread affinity: Tk objects are created and touched on exactly one thread.
  When embedded, that is your main thread. When `AvatarController.run_in_thread()`
  is used, the whole Tk world (root, view, PhotoImage, after) lives in the one
  dedicated thread and nothing else ever touches it.

Run standalone to preview::

    python -m gui.avatar
"""
from __future__ import annotations

import io
import logging
import math
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger("zara.avatar")

# ── Optional dependencies ───────────────────────────────────────────────────
# Missing Pillow is a hard dependency for the GIF path, but NOT for the module:
# without it we simply fall back to the vector ring. Import errors are captured
# so a broken install degrades instead of crashing the app at import time.
try:
    from PIL import Image, ImageSequence
    _PIL_OK = True
    _PIL_ERR: Optional[BaseException] = None
except Exception as _e:  # pragma: no cover - environment dependent
    Image = None  # type: ignore
    ImageSequence = None  # type: ignore
    _PIL_OK = False
    _PIL_ERR = _e

try:
    import tkinter as tk
    _TK_OK = True
    _TK_ERR: Optional[BaseException] = None
except Exception as _e:  # pragma: no cover - environment dependent
    tk = None  # type: ignore
    _TK_OK = False
    _TK_ERR = _e


# ── State vocabulary ────────────────────────────────────────────────────────
# The canonical states Zara's backend dispatches. These are the exact strings
# carried through the event bus, so a spelling mistake here is a state that
# silently never lights up. Keep them in sync with main.py's STATE_* constants.
IDLE = "Idle"
LISTENING = "Listening"
PROCESSING = "Processing"
RESEARCHING = "Researching"
WRITING = "Writing"
SPEAKING = "Speaking"
# Zara is on a phone call on the owner's behalf (skills/whatsapp_call_assistant).
# A distinct state rather than a long SPEAKING, because the two most important
# things about it are the opposite: nothing is being typed, and the microphone is
# the caller's, not the owner's. skills/… owns when it changes; this only defines
# that the state exists.
IN_CALL = "In Call"

STATES: tuple[str, ...] = (
    IDLE, LISTENING, PROCESSING, RESEARCHING, WRITING, SPEAKING, IN_CALL,
)

# Aliases so the older HUD vocabulary ("THINKING", "LISTENING", "SLEEPING")
# still maps onto a sensible avatar state rather than one it has never heard of.
_STATE_ALIASES: dict[str, str] = {
    "INITIALISING": PROCESSING,
    "INITIALIZING": PROCESSING,
    "THINKING": PROCESSING,
    "PROCESSING": PROCESSING,
    "LISTENING": LISTENING,
    "SLEEPING": IDLE,
    "ASLEEP": IDLE,
    "IDLE": IDLE,
    "RESEARCHING": RESEARCHING,
    "RESEARCH": RESEARCHING,
    "WRITING": WRITING,
    "WRITE": WRITING,
    "SPEAKING": SPEAKING,
    "IN_CALL": IN_CALL,
    "INCALL": IN_CALL,
    "ON CALL": IN_CALL,
    "MUTED": IDLE,
}

# Distinct accent per state — deliberately the same family the PyQt HUD uses
# (ui.py class C) so the two surfaces read as one product.
STATE_COLORS: dict[str, str] = {
    IDLE:        "#00d4ff",   # cyan   — C.PRI
    LISTENING:   "#00ff88",   # green  — C.GREEN
    PROCESSING:  "#7aa2ff",   # blue
    RESEARCHING: "#b07aff",   # violet
    WRITING:     "#ffcc00",   # amber  — C.ACC2
    SPEAKING:    "#ff6b00",   # orange — C.ACC
    IN_CALL:     "#00d4ff",   # cyan   — the same accent as IDLE on purpose:
                              # a call is a passive state, and a third hue here
                              # would read as "something is being computed".
}

HUD_BG = "#00060a"            # C.BG


def _fit_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Largest (w, h) that fits inside (box_w, box_h) preserving aspect ratio."""
    if src_w <= 0 or src_h <= 0:
        return max(1, box_w), max(1, box_h)
    scale = min(box_w / src_w, box_h / src_h)
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def normalize_state(state: str) -> str:
    """Map any incoming state string onto one of STATES.

    Never raises and never returns an unknown value: an unrecognised string
    becomes IDLE rather than leaving the avatar stuck on its last colour.
    """
    if not state:
        return IDLE
    s = str(state).strip()
    if s in STATES:
        return s
    return _STATE_ALIASES.get(s.upper(), IDLE)


# ── Avatar asset resolution ─────────────────────────────────────────────────
DEFAULT_GIF_NAME = "sara_avatar.gif"

# Per-state preferences, best first. A state's own `sara_<state>.gif` wins; then
# the generic `sara_avatar.gif` (so a single supplied clip drives every state);
# then the clips that already ship in assists/GIF.
STATE_GIF_CANDIDATES: dict[str, tuple[str, ...]] = {
    IDLE:        (DEFAULT_GIF_NAME, "zara_idle.gif", "sara_idle.gif",
                  "idle.gif", "Just wating.gif", "widget.gif"),
    LISTENING:   ("sara_listening.gif", DEFAULT_GIF_NAME, "zara_idle.gif",
                  "idle.gif", "Just wating.gif", "widget.gif"),
    PROCESSING:  ("sara_thinking.gif", DEFAULT_GIF_NAME,
                  "thinking.gif", "idle.gif"),
    RESEARCHING: ("sara_thinking.gif", "sara_researching.gif", DEFAULT_GIF_NAME,
                  "thinking.gif", "idle.gif"),
    WRITING:     ("sara_writing.gif", "sara_thinking.gif", DEFAULT_GIF_NAME,
                  "thinking.gif", "idle.gif"),
    SPEAKING:    ("sara_speaking.gif", "sara_talking.gif", DEFAULT_GIF_NAME,
                  "talking.gif", "idle.gif"),
    # No dedicated clip is expected here. A call has no pose of its own — the
    # caller's turn is LISTENING and Zara's is SPEAKING — so this resolves to
    # the same generic clip every other state falls back to.
    IN_CALL:     (DEFAULT_GIF_NAME, "zara_idle.gif", "sara_idle.gif",
                  "idle.gif", "Just wating.gif", "widget.gif"),
}


# Transient gestures played *on top of* the active clip (a wave on hello/bye).
# A dedicated clip per gesture is optional: when none is present the gesture is
# simply skipped and the avatar carries on unchanged.
GESTURE_GIF_CANDIDATES: dict[str, tuple[str, ...]] = {
    "wave": ("zara_wave.gif", "wave.gif", "sara_wave.gif", "waving.gif", "greeting.gif",
             "bye.gif", "goodbye.gif"),
    "bye": ("zara_bye.gif", "bye.gif", "goodbye.gif", "zara_wave.gif", "wave.gif"),
}


def default_base_dir() -> Path:
    """Project root — the folder that contains `assists/` and `main.py`."""
    return Path(__file__).resolve().parent.parent


def _candidate_roots(base_dir: Optional[Path] = None) -> list[Path]:
    base = Path(base_dir) if base_dir else default_base_dir()
    return [base / "assists" / "GIF", base / "assists"]


def _index_gifs(base_dir: Optional[Path] = None) -> dict[str, Path]:
    """A case-insensitive {lowercased name: path} index of every GIF in the
    assist folders. Built once per call; the folders hold a handful of files."""
    index: dict[str, Path] = {}
    for root in _candidate_roots(base_dir):
        try:
            if not root.is_dir():
                continue
            for p in root.iterdir():
                if p.is_file() and p.suffix.lower() == ".gif":
                    index.setdefault(p.name.lower(), p)
        except OSError:
            continue
    return index


def resolve_state_gifs(base_dir: Optional[Path] = None) -> dict[str, Optional[Path]]:
    """Resolve the best GIF for each state.

    Returns {state: path-or-None}. A state resolves to None only when no GIF at
    all is available, which is the signal to use the vector-ring fallback.
    """
    index = _index_gifs(base_dir)
    resolved: dict[str, Optional[Path]] = {}
    for state, names in STATE_GIF_CANDIDATES.items():
        found: Optional[Path] = None
        for name in names:
            hit = index.get(name.lower())
            if hit is not None:
                found = hit
                break
        resolved[state] = found
    return resolved


def resolve_gesture_gifs(base_dir: Optional[Path] = None) -> dict[str, Optional[Path]]:
    """Resolve the optional clip for each gesture name (None when absent)."""
    index = _index_gifs(base_dir)
    resolved: dict[str, Optional[Path]] = {}
    for gesture, names in GESTURE_GIF_CANDIDATES.items():
        found: Optional[Path] = None
        for name in names:
            hit = index.get(name.lower())
            if hit is not None:
                found = hit
                break
        resolved[gesture] = found
    return resolved


def find_primary_gif(base_dir: Optional[Path] = None) -> Optional[Path]:
    """The single clip that should be preloaded at startup (if any)."""
    index = _index_gifs(base_dir)
    for name in (DEFAULT_GIF_NAME, "idle.gif", "just wating.gif", "widget.gif"):
        hit = index.get(name.lower())
        if hit is not None:
            return hit
    # Any GIF at all is better than the ring.
    return next(iter(index.values()), None)


# ── Events ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AvatarEvent:
    """One state change, produced on a backend thread and consumed on the Tk
    thread. `level` is an optional 0.0–1.0 loudness for the speaking state."""
    state: str
    level: float = 0.0
    source: str = ""
    ts: float = field(default_factory=time.monotonic)


class AvatarEventBus:
    """Thread-safe hand-off between backend threads and the GUI thread.

    This is the only object shared across threads. `emit()` is safe from any
    thread (including the audio callback); `drain()` must only be called from
    the Tk thread. The queue is bounded so a hot loop cannot grow without limit:
    under back-pressure the OLDEST event is dropped, because for a state display
    the newest state is always the one that matters.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._q: "queue.Queue[AvatarEvent]" = queue.Queue(maxsize=maxsize)
        self._latest: Optional[AvatarEvent] = None
        self._level: float = 0.0
        self._lock = threading.Lock()

    def emit(self, state: str, level: float = 0.0, source: str = "") -> None:
        ev = AvatarEvent(state=normalize_state(state), level=float(level), source=source)
        with self._lock:
            self._latest = ev
            if level:
                self._level = max(0.0, min(1.0, float(level)))
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            # Drop the oldest so the newest state still gets through.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(ev)
            except queue.Full:
                pass

    def drain(self, limit: int = 64) -> list[AvatarEvent]:
        out: list[AvatarEvent] = []
        for _ in range(max(0, limit)):
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    @property
    def latest(self) -> Optional[AvatarEvent]:
        with self._lock:
            return self._latest

    def set_level(self, level: float) -> None:
        """Thread-safe loudness feed for the ring. Kept off the event queue on
        purpose: the audio path writes this dozens of times a second and a state
        event per sample would flood the queue and stall the display."""
        try:
            v = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._level = v

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    def clear(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._latest = None


# ── GIF loading ─────────────────────────────────────────────────────────────
class AvatarLoadError(RuntimeError):
    """Raised when a GIF cannot be decoded. Callers fall back to the ring."""


class GifFrameStore:
    """A GIF decoded, resized and re-encoded into PNG bytes held in RAM.

    Only bytes are retained after `load()`. The PIL image is closed at the end
    of loading, so peak memory is one decoded frame at a time plus the output
    list — not the whole RGBA stack.
    """

    _CACHE_LIMIT = 3

    def __init__(self) -> None:
        self.source: Optional[Path] = None
        self.width = 0
        self.height = 0
        self.durations: list[float] = []      # seconds, one per frame
        self.total_duration = 0.0
        self._by_size: "OrderedDict[tuple[int, int, str, int], list[bytes]]" = OrderedDict()
        self._lock = threading.Lock()

    # -- properties ---------------------------------------------------------
    @property
    def n_frames(self) -> int:
        return len(self.durations)

    def png_frames(self, width: int, height: int,
                   *, flatten: Optional[str] = None,
                   quality: int = 1) -> list[bytes]:
        """PNG bytes for every frame at the requested canvas size.

        Results are cached per (width, height, flatten, quality). A resize is
        re-decoded from disk rather than kept full-resolution in memory, which
        is what keeps the resident cost to a few MB instead of the whole RGBA
        stack. The cache is guarded so a background resize thread and the Tk
        thread cannot corrupt it.
        """
        if not self.source:
            return []
        key = (int(width), int(height), flatten or "", int(quality))
        with self._lock:
            hit = self._by_size.get(key)
            if hit is not None:
                self._by_size.move_to_end(key)
                return hit
        frames = self._decode(int(width), int(height), flatten, quality)
        with self._lock:
            self._by_size[key] = frames
            while len(self._by_size) > self._CACHE_LIMIT:
                self._by_size.popitem(last=False)
        return frames

    # -- loading ------------------------------------------------------------
    def cached_frames(self, width: int, height: int,
                      flatten: Optional[str] = None,
                      quality: int = 1) -> Optional[list[bytes]]:
        """Return already-decoded frames for a size, or None. Never decodes, so
        it is safe to call on the Tk thread without risking a freeze."""
        key = (int(width), int(height), flatten or "", int(quality))
        with self._lock:
            hit = self._by_size.get(key)
            if hit is not None:
                self._by_size.move_to_end(key)
            return hit

    def load(self, path: os.PathLike | str, width: int, height: int,
             *, flatten: Optional[str] = None, interpolate: bool = False,
             target_fps: float = 28.0, max_frames: int = 400,
             quality: int = 1) -> None:
        """Decode `path` at startup and warm the cache. Raises AvatarLoadError
        on any failure; the caller then falls back to the vector ring."""
        if not _PIL_OK:
            raise AvatarLoadError(f"Pillow unavailable: {_PIL_ERR}")
        p = Path(path)
        if not p.is_file():
            raise AvatarLoadError(f"GIF not found: {p}")

        durations, source_size = self._read_durations(p)
        if not durations:
            raise AvatarLoadError(f"GIF has no frames: {p}")

        if interpolate:
            durations = self._upsample_timings(durations, target_fps, max_frames)

        self.source = p
        self.width = int(width)
        self.height = int(height)
        self.durations = durations
        self.total_duration = float(sum(durations))
        with self._lock:
            self._by_size.clear()
        # Decode exactly once, now, and store it under the same key png_frames()
        # will ask for. Decoding here and then again on first paint (the previous
        # behaviour) doubled startup for no reason.
        try:
            frames = self._decode(self.width, self.height, flatten, quality)
        except Exception as e:
            self.source = None
            raise AvatarLoadError(f"GIF decode failed ({p.name}): {e}") from e
        if not frames:
            self.source = None
            raise AvatarLoadError(f"GIF produced no frames: {p.name}")
        with self._lock:
            self._by_size[(self.width, self.height, flatten or "", int(quality))] = frames

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _read_durations(path: Path) -> tuple[list[float], tuple[int, int]]:
        """Frame delays in seconds, using ImageSequence exactly as the spec
        requires. Also returns the native pixel size."""
        durations: list[float] = []
        size = (0, 0)
        try:
            with Image.open(path) as im:
                size = im.size
                for frame in ImageSequence.Iterator(im):
                    ms = frame.info.get("duration", 0) or im.info.get("duration", 0) or 100
                    try:
                        ms = int(ms)
                    except (TypeError, ValueError):
                        ms = 100
                    # A zero delay means "as fast as possible"; clamp to 20 ms so
                    # the scheduler is never asked to spin at 1000 Hz.
                    durations.append(max(0.02, ms / 1000.0))
        except FileNotFoundError:
            raise
        except Exception as e:
            raise AvatarLoadError(f"cannot read {path.name}: {e}") from e
        if not durations:
            durations = [0.1]
        return durations, size

    def _decode(self, width: int, height: int,
                flatten: Optional[str], quality: int) -> list[bytes]:
        assert Image is not None and ImageSequence is not None
        width = max(1, int(width))
        height = max(1, int(height))
        target = (width, height)
        out: list[bytes] = []
        with Image.open(self.source) as im:  # type: ignore[arg-type]
            # The source frame size is constant, so the fit box is computed once
            # rather than per frame. All frames are then pasted at the same
            # offset onto a canvas of exactly the requested size, which removes
            # all per-frame position maths from the paint path.
            src_w, src_h = im.size
            fitted_w, fitted_h = _fit_size(src_w, src_h, width, height)
            off = ((width - fitted_w) // 2, (height - fitted_h) // 2)
            for frame in ImageSequence.Iterator(im):
                rgba = frame.convert("RGBA")
                if (fitted_w, fitted_h) != (src_w, src_h):
                    rgba = rgba.resize((fitted_w, fitted_h), Image.LANCZOS)
                canvas = Image.new("RGBA", target, (0, 0, 0, 0))
                canvas.paste(rgba, off)
                if flatten:
                    flat = Image.new("RGBA", target, flatten)
                    flat.alpha_composite(canvas)
                    canvas = flat
                buf = io.BytesIO()
                # compress_level=1: PNG encoding was the single largest cost in
                # startup. Level 1 is a fraction of the time of level 6 for a
                # slightly larger blob, and these bytes never leave RAM.
                canvas.save(buf, format="PNG", compress_level=quality)
                out.append(buf.getvalue())
        return out

    @staticmethod
    def _upsample_timings(durations: list[float], target_fps: float,
                          max_frames: int) -> list[float]:
        """Uniform per-frame timings at up to `target_fps`, preserving the loop
        length. Motion smoothing itself is out of scope for the timing pass —
        the scheduler simply holds/advances against these values."""
        total = sum(durations)
        if total <= 0:
            return durations
        fps = max(1.0, min(float(target_fps), 60.0))
        step = 1.0 / fps
        n = max(1, min(max_frames, int(round(total / step))))
        return [total / n] * n

    # -- inspection ---------------------------------------------------------
    def describe(self) -> str:
        if not self.source:
            return "no gif"
        return (f"{self.source.name} {self.width}x{self.height} "
                f"{self.n_frames}f {self.total_duration:.2f}s")


# ── Vector-ring fallback ────────────────────────────────────────────────────
class PulsingRing:
    """The graceful-degradation centrepiece.

    Draws an animated vector ring directly onto a Tk canvas: rotating arc
    segments, a pulsing core and a loudness-reactive halo. It exists so the
    panel is never empty when the GIF is missing or unreadable.
    """

    def __init__(self) -> None:
        self.phase = 0.0
        self.pulse = 0.0

    def step(self, dt: float, level: float, state: str) -> None:
        state = normalize_state(state)
        speed = {
            IDLE: 0.6, LISTENING: 1.1, PROCESSING: 2.4,
            RESEARCHING: 2.4, WRITING: 1.6, SPEAKING: 3.2,
        }.get(state, 1.0)
        self.phase = (self.phase + dt * speed) % (2.0 * math.pi)
        target = 0.35 + 0.65 * max(0.0, min(1.0, level))
        self.pulse += (target - self.pulse) * min(1.0, dt * 6.0)

    def paint(self, canvas, cx: float, cy: float, radius: float, color: str) -> None:
        canvas.delete("ring")
        r = max(4.0, radius)
        p = self.pulse
        # Halo: a few faint outlines rather than a real gradient (Tk has none).
        for i, (scale, width) in enumerate(((0.78, 2), (0.88, 1), (1.0, 1))):
            rr = r * scale * (1.0 + 0.06 * p)
            canvas.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                               outline=color, width=width,
                               stipple="gray25" if i == 2 else "",
                               tags="ring")
        # Two counter-rotating arcs make the ring read as "alive".
        a1 = math.degrees(self.phase) % 360.0
        a2 = math.degrees(-self.phase * 1.4) % 360.0
        r_arc = r * (0.9 + 0.08 * p)
        canvas.create_arc(cx - r_arc, cy - r_arc, cx + r_arc, cy + r_arc,
                          start=a1, extent=110, style=tk.ARC, outline=color,
                          width=max(2, int(r * 0.045)), tags="ring")
        r_arc2 = r * (1.06 + 0.05 * p)
        canvas.create_arc(cx - r_arc2, cy - r_arc2, cx + r_arc2, cy + r_arc2,
                          start=a2, extent=70, style=tk.ARC, outline=color,
                          width=max(1, int(r * 0.025)), tags="ring")
        # Orbiting node.
        ox = cx + math.cos(self.phase) * r_arc2
        oy = cy + math.sin(self.phase) * r_arc2
        node = max(3, int(r * 0.05))
        canvas.create_oval(ox - node, oy - node, ox + node, oy + node,
                           fill=color, outline="", tags="ring")
        # Pulsing core.
        core = r * (0.16 + 0.10 * p)
        canvas.create_oval(cx - core, cy - core, cx + core, cy + core,
                           outline=color, width=2, tags="ring")
        inner = max(2, int(core * 0.45))
        canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner,
                           fill=color, outline="", tags="ring")


# ── The Tk widget ───────────────────────────────────────────────────────────
class AvatarView(tk.Frame if _TK_OK else object):  # type: ignore[misc]
    """A Tk widget that shows the looping GIF, or the pulsing ring.

    The animation is driven entirely by `self.after()` on whichever thread
    created the widget. Feed it states with `set_state()` (safe from the Tk
    thread) or, preferably, hand it an AvatarEventBus via a controller.
    """

    def __init__(
        self,
        master,
        *,
        base_dir: Optional[Path] = None,
        width: int = 440,
        height: int = 300,
        fps: float = 28.0,
        background: str = HUD_BG,
        flatten: Optional[str] = None,
        interpolate: bool = False,
        state_gifs: bool = False,
        show_label: bool = True,
        autostart: bool = True,
        **kw,
    ) -> None:
        super().__init__(master, bg=background, **kw)
        if not _TK_OK:
            raise AvatarLoadError(f"tkinter unavailable: {_TK_ERR}")

        self.base_dir = Path(base_dir) if base_dir else default_base_dir()
        self.fps = max(1.0, min(float(fps), 60.0))
        self._interval_ms = max(16, int(round(1000.0 / self.fps)))
        self.background = background
        self.flatten = flatten
        self.interpolate = interpolate
        self.state_gifs = state_gifs
        self.show_label = show_label

        self.state = IDLE
        self.level = 0.0
        self.graph = PulsingRing()
        self.load_status = "uninitialised"
        self.using_gif = False

        self._canvas = tk.Canvas(self, bg=background, highlightthickness=0,
                                 bd=0)
        self._canvas.pack(fill="both", expand=True)
        self._label_id = None
        if show_label:
            self._label_id = self._canvas.create_text(
                0, 0, text="", anchor="s", fill=STATE_COLORS[IDLE],
                font=("Consolas", 11, "bold"), tags="label")

        self._gif_paths = resolve_state_gifs(self.base_dir)
        self._store: Optional[GifFrameStore] = None
        self._store_key: Optional[str] = None
        self._stores: dict[str, GifFrameStore] = {}
        self._png: list[bytes] = []
        self._photo: list = []          # tk.PhotoImage refs (must be kept alive)
        self._size = (int(width), int(height))
        self._built_size = None
        self._idx = 0
        self._elapsed = 0.0
        self._drawn = -1
        self._t0 = time.monotonic()
        self._last = self._t0
        self._after_id = None
        self._running = False
        self._resize_job = None
        self._glance_offset = 0.0
        self._glance_until = 0.0

        # Cross-thread hand-off. Decoding a 50-frame GIF takes long enough to be
        # visible as a freeze, so the heavy work runs on a loader thread and the
        # Tk `_tick` (which is already running every ~35 ms) picks the result up
        # on the GUI thread. Tk objects are NEVER touched off-thread.
        self._pending_lock = threading.Lock()
        self._pending_load: Optional[tuple[str, Optional[GifFrameStore], str]] = None
        self._pending_frames: Optional[tuple[int, int, int, GifFrameStore, list[bytes]]] = None
        self._load_token = 0
        self._loading = False

        self.bind("<Configure>", self._on_configure)
        # The real canvas size is only known once the widget is mapped, and a
        # 320x220-ish guess that the window manager immediately resizes to
        # 378x265 would cost a full second decode for nothing. So the load waits
        # briefly for the first Configure, with a timer as a safety net for the
        # never-mapped case.
        self._load_started = False
        self._load_trigger = self.after(120, self._ensure_load_started)

        if autostart:
            self.start()

    # -- asset loading ------------------------------------------------------
    def _ensure_load_started(self) -> None:
        self._load_trigger = None
        if self._load_started:
            return
        self._load_started = True
        self._start_primary_load()

    def _start_primary_load(self) -> None:
        """Kick off the startup load on a worker thread. The window shows the
        ring immediately and the GIF takes over the moment it is ready."""
        primary = find_primary_gif(self.base_dir)
        if primary is None:
            self._fallback("no gif found; using vector ring")
            return
        w, h = self._size
        self._loading = True

        def _work() -> None:
            try:
                store = self._make_store(primary, w, h)
                status = f"gif: {store.describe()}"
            except Exception as e:
                store, status = None, f"gif unusable ({e}); using vector ring"
            with self._pending_lock:
                self._pending_load = ("__primary__", store, status)

        threading.Thread(target=_work, name="zara-avatar-load",
                         daemon=True).start()

    def _make_store(self, path: Path, w: int, h: int) -> GifFrameStore:
        store = GifFrameStore()
        store.load(path, w, h, flatten=self.flatten,
                   interpolate=self.interpolate, target_fps=self.fps)
        return store

    def _fallback(self, why: str) -> None:
        self._store = None
        self.using_gif = False
        self.load_status = why
        logger.warning("[Avatar] %s", why)

    def _activate_store(self, store: GifFrameStore) -> None:
        """Make `store` the active clip (Tk thread only). Reuses the frames the
        loader already decoded when it can, otherwise asks for an async rebuild."""
        self._store = store
        self.using_gif = True
        self._built_size = None
        self._png = []
        self._photo = []
        self._idx = 0
        self._elapsed = 0.0
        self._drawn = -1
        w, h = self._size
        frames = store.cached_frames(w, h, self.flatten)
        if frames:
            self._png = frames
            self._photo = [self._photo_from_bytes(b) for b in frames]
            self._built_size = (w, h)
        elif not self._loading:
            self._schedule_frames_build(store, w, h)

    def _on_configure(self, event) -> None:
        if event.width <= 1 or event.height <= 1:
            return
        self._size = (event.width, event.height)
        # First real size: start the load here instead of at the guessed size.
        if not self._load_started:
            if self._load_trigger is not None:
                try:
                    self.after_cancel(self._load_trigger)
                except Exception:
                    pass
                self._load_trigger = None
            self._ensure_load_started()
            return
        if not self.using_gif or self._store is None:
            return
        # Rebuild off a short debounce — a window drag fires Configure dozens of
        # times a second and re-decoding 50 frames each time makes the panel
        # stutter while resizing.
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(260, self._rebuild_for_size)

    def _rebuild_for_size(self) -> None:
        self._resize_job = None
        if not self.using_gif or self._store is None:
            return
        w, h = self._size
        if self._built_size == (w, h):
            return
        self._schedule_frames_build(self._store, w, h)

    def _schedule_frames_build(self, store: GifFrameStore, w: int, h: int) -> None:
        """Decode frames for a new size off-thread; `_apply_pending` installs
        them. A token drops results from a size that has already been superseded."""
        self._load_token += 1
        token = self._load_token

        def _work() -> None:
            try:
                frames = store.png_frames(w, h, flatten=self.flatten)
            except Exception as e:  # pragma: no cover - best effort
                logger.warning("[Avatar] frame build failed: %s", e)
                return
            with self._pending_lock:
                self._pending_frames = (token, w, h, store, frames)

        threading.Thread(target=_work, name="zara-avatar-resize",
                         daemon=True).start()

    def _apply_pending(self) -> None:
        """Called on the Tk thread each tick. Installs any finished worker
        result. This is the only place a loader result reaches a Tk object."""
        with self._pending_lock:
            load = self._pending_load
            self._pending_load = None
            pending = self._pending_frames
            self._pending_frames = None

        if load is not None:
            key, store, status = load
            self._loading = False
            if store is None:
                self._fallback(status)
            else:
                self._stores[key] = store
                self._store_key = key
                self.load_status = status
                self._activate_store(store)

        if pending is not None:
            token, w, h, store, frames = pending
            if token == self._load_token and frames and store is self._store \
                    and (w, h) == self._size:
                self._png = frames
                self._photo = [self._photo_from_bytes(b) for b in frames]
                self._built_size = (w, h)
                self._drawn = -1
                self._draw()

    def _photo_from_bytes(self, data: bytes):
        try:
            return tk.PhotoImage(data=data)
        except Exception:
            return None

    # -- public control (Tk thread) -----------------------------------------
    def set_state(self, state: str) -> None:
        new = normalize_state(state)
        if new == self.state:
            return
        self.state = new
        self._switch_store_for_state(new)
        try:
            self._canvas.itemconfigure(self._label_id, fill=STATE_COLORS[new])
        except Exception:
            pass
        self._drawn = -1
        if not self._running:
            self._draw()

    def set_level(self, level: float) -> None:
        try:
            self.level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self.level = 0.0

    def glance(self, dx: float) -> None:
        """A small horizontal nudge used when Zara 'looks' somewhere."""
        self._glance_offset = max(-1.0, min(1.0, float(dx))) * 0.04
        self._glance_until = time.monotonic() + 1.0

    def _switch_store_for_state(self, state: str) -> None:
        """Swap in a state-specific clip when one exists.

        A single sara_avatar.gif drives every state, so in the common case this
        does nothing. Only when the primary is one of the bundled state clips
        (idle/talking/thinking) does the avatar actually change clip.

        Per-state clips are opt-in (`state_gifs=True`) because each distinct
        clip has to be decoded, and a state change that arrives while a decode
        is still running briefly leaves the panel on the ring. The default of a
        single looping clip is both the spec and the smoothest behaviour.
        """
        if not self.using_gif or not self.state_gifs:
            return
        primary = self._stores.get("__primary__")
        if primary is not None and primary.source is not None and \
                primary.source.name.lower() == DEFAULT_GIF_NAME.lower():
            return
        path = self._gif_paths.get(state)
        if path is None:
            return
        key = str(path)
        store = self._stores.get(key)
        if store is not None:
            if store is not self._store:
                self._activate_store(store)
            return
        # Not loaded yet — load it off-thread and switch when it arrives.
        w, h = self._size
        self._loading = True

        def _work() -> None:
            try:
                s = self._make_store(path, w, h)
                status = f"gif: {s.describe()}"
            except Exception as e:
                s, status = None, f"state gif {path.name} failed: {e}"
            with self._pending_lock:
                self._pending_load = (key, s, status)

        threading.Thread(target=_work, name="zara-avatar-state",
                         daemon=True).start()

    # -- animation loop -----------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last = time.monotonic()
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _tick(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        try:
            self._apply_pending()      # install any finished background decode
            self._advance(dt)
        except Exception as e:  # never let one bad frame kill the loop
            logger.debug("[Avatar] tick error: %s", e)
        self._after_id = self.after(self._interval_ms, self._tick)

    def _advance(self, dt: float) -> None:
        if self._glance_offset and time.monotonic() > self._glance_until:
            self._glance_offset = 0.0
        # No clip (or one still decoding): draw the ring as a live placeholder.
        if not self.using_gif or self._store is None or not self._png:
            self.graph.step(dt, self.level, self.state)
            self._draw()
            if self.using_gif and self._store is not None \
                    and self._built_size != self._size and not self._loading:
                # A resize landed before frames were built — ask for them.
                self._schedule_frames_build(self._store, *self._size)
            return

        store = self._store
        n = len(self._png)
        durations = store.durations
        self._elapsed += dt
        # Skip forward across frames whose display time has fully elapsed.
        if self._idx >= len(durations):
            self._idx = 0
        safety = 0
        while self._elapsed >= durations[self._idx] and safety < 1000:
            self._elapsed -= durations[self._idx]
            self._idx = (self._idx + 1) % len(durations)
            safety += 1
        if self._idx != self._drawn or n != len(self._photo):
            self._draw()

    def _draw(self) -> None:
        c = self._canvas
        if not self.using_gif or self._store is None or not self._png:
            w, h = self._size
            cx = w / 2.0 + self._glance_offset * w
            cy = h / 2.0
            radius = min(w, h) * (0.30 if self.show_label else 0.40)
            self.graph.paint(c, cx, cy, radius, STATE_COLORS[self.state])
            self._draw_label(w, h)
            return
        self._drawn = self._idx
        c.delete("gif")
        photo = self._photo[self._idx] if self._idx < len(self._photo) else None
        if photo is None:
            return
        w, h = self._size
        x = w / 2.0 + self._glance_offset * w
        c.create_image(x, h / 2.0, image=photo, anchor="center", tags="gif")
        self._draw_label(w, h)

    def _draw_label(self, w: int, h: int) -> None:
        if self._label_id is None:
            return
        try:
            self._canvas.coords(self._label_id, w / 2.0, h - 8)
            self._canvas.itemconfigure(
                self._label_id, text=self.state.upper(),
                fill=STATE_COLORS[self.state])
            self._canvas.tag_raise(self._label_id)
        except Exception:
            pass

    def release_images(self) -> None:
        """Drop PhotoImage references. Must be called on the Tk thread so Tcl
        tears them down there instead of during a GC pass on another thread."""
        self._photo = []
        self._png = []

    def destroy(self) -> None:  # pragma: no cover - teardown
        self.stop()
        self.release_images()
        super().destroy()


# ── Controller ──────────────────────────────────────────────────────────────
class AvatarController:
    """Owns the bus, the view and the `after()` pump.

    Two usage modes:

    Embedded — you already have a Tk root on your main thread::

        av = AvatarController(bus=ZARA_AVATAR_BUS)
        view = av.mount(root)
        av.start(root)          # begins the after() pump

    Standalone — Zara's app loop is not Tk, so the avatar owns its own thread::

        av = AvatarController(bus=ZARA_AVATAR_BUS)
        av.run_in_thread(title="Zara Avatar", width=440, height=320)

    In both modes, backend threads call `bus.emit("Speaking")` and never touch a
    widget.
    """

    def __init__(self, bus: Optional[AvatarEventBus] = None, **view_kwargs) -> None:
        self.bus = bus if bus is not None else AvatarEventBus()
        self.view_kwargs = view_kwargs
        self.view: Optional[AvatarView] = None
        self._root = None
        self._after_id = None
        self._thread: Optional[threading.Thread] = None
        self._tk_thread: Optional[threading.Thread] = None
        self._stopped = False
        self._close_requested = False
        self._level = 0.0

    # -- embedded -----------------------------------------------------------
    def mount(self, master) -> AvatarView:
        self.view = AvatarView(master, **self.view_kwargs)
        self.view.pack(fill="both", expand=True)
        return self.view

    def start(self, root) -> None:
        self._root = root
        self._tk_thread = threading.current_thread()
        self._stopped = False
        self._close_requested = False
        self._pump()

    def stop(self) -> None:
        """Stop the pump and animation. Safe to call from any thread: only the
        thread that owns the Tk root is permitted to touch `after_cancel`, so
        from anywhere else this just raises the flag for that thread to see."""
        self._stopped = True
        if self.view is not None:
            self.view.stop()
        if self._root is None or self._after_id is None:
            return
        if threading.current_thread() is self._tk_thread:
            try:
                self._root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def close(self) -> None:
        """Request shutdown. If it owns its thread the root is destroyed there;
        from another thread this only sets a flag the pump acts on."""
        self._close_requested = True
        if threading.current_thread() is self._tk_thread and self._root is not None:
            self._destroy_root()

    def _destroy_root(self) -> None:
        self._stopped = True
        if self.view is not None:
            self.view.stop()
            # Drop PhotoImage references on the Tk thread so Tcl frees them
            # here rather than in a GC pass on some unrelated thread.
            self.view.release_images()
        root = self._root
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    def _pump(self) -> None:
        if self._close_requested:
            self._destroy_root()
            return
        if self._stopped or self._root is None:
            return
        try:
            for ev in self.bus.drain():
                if self.view is None:
                    continue
                self.view.set_state(ev.state)
                if ev.level:
                    self._level = ev.level
            if self.view is not None:
                # Prefer the live level feed; fall back to the last event's level.
                live = self.bus.level
                if live:
                    self._level = live
                elif self.view.state == SPEAKING:
                    self._level *= 0.94
                self.view.set_level(self._level)
        except Exception as e:
            # One bad state must never stop the pump — if it did, the avatar
            # would freeze on its last frame and never respond again.
            logger.debug("[Avatar] pump error: %s", e)
        self._after_id = self._root.after(30, self._pump)

    # -- standalone ---------------------------------------------------------
    def run_in_thread(self, *, title: str = "Zara",
                      width: int = 440, height: int = 300,
                      daemon: bool = True,
                      on_ready: Optional[Callable[[], None]] = None) -> threading.Thread:
        """Run the avatar in a dedicated daemon thread with its own Tk root.

        The whole Tk universe (root, view, PhotoImages, after callbacks) is
        created and used in that one thread; no other thread ever touches it.
        """
        if not _TK_OK:
            raise AvatarLoadError(f"tkinter unavailable: {_TK_ERR}")
        self.view_kwargs.setdefault("width", width)
        self.view_kwargs.setdefault("height", height)

        def _main() -> None:
            try:
                root = tk.Tk()
            except Exception as e:  # pragma: no cover
                logger.error("[Avatar] Tk root failed: %s", e)
                return
            self._root = root
            root.title(title)
            root.configure(bg=HUD_BG)
            try:
                root.attributes("-topmost", False)
            except Exception:
                pass
            self.mount(root)
            root.protocol("WM_DELETE_WINDOW", self._on_window_close)
            self.start(root)
            if on_ready is not None:
                try:
                    on_ready()
                except Exception:
                    pass
            root.mainloop()

        self._thread = threading.Thread(target=_main, name="zara-avatar",
                                        daemon=daemon)
        self._thread.start()
        return self._thread

    def _on_window_close(self) -> None:
        self.close()


# ── Standalone preview ──────────────────────────────────────────────────────
def run_standalone(base_dir: Optional[Path] = None) -> None:
    """A small window with buttons for each state — for previewing the avatar."""
    if not _TK_OK:
        raise SystemExit(f"tkinter unavailable: {_TK_ERR}")

    root = tk.Tk()
    root.title("Zara Avatar — standalone preview")
    root.configure(bg=HUD_BG)

    bus = AvatarEventBus()
    controller = AvatarController(bus=bus, base_dir=base_dir,
                                  width=460, height=320)
    view = controller.mount(root)

    bar = tk.Frame(root, bg=HUD_BG)
    bar.pack(fill="x", side="bottom")
    for state in STATES:
        tk.Button(
            bar, text=state, command=lambda s=state: bus.emit(s),
            bg="#02131c", fg=STATE_COLORS[state],
            activebackground="#063040", activeforeground="#ffffff",
            relief="flat", padx=10, pady=4, bd=0,
        ).pack(side="left", padx=2, pady=6)

    controller.start(root)
    root.mainloop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    run_standalone()
