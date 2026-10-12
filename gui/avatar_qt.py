"""In-HUD animated GIF avatar for the PyQt interface (ui.py).

This is the renderer that *replaces* the 3-D wireframe head drawn by
``core/avatar.py`` inside ``HudCanvas``. It paints the same head band, but with
the frames of a real GIF, and it is driven off the very same live signals the
wireframe used:

    hud.state      -> which clip plays (Idle / Listening / Thinking / Speaking)
    hud.speaking   -> forces the talking clip and the speaking cadence
    hud._amp_disp  -> smoothed 0..1 audio level (mic while listening, Zara's own
                      voice while speaking — see HudCanvas.set_audio_level)
    push_visemes() -> per-20 ms (level, openness, width) frames, used to bias the
                      talking clip's playback so the mouth tracks the words

Why the decode is asynchronous
------------------------------
A 50-frame 800x457 GIF takes a couple of seconds to decode with Pillow. Doing
that on the Qt thread would freeze the window on startup, so the clips are
decoded on a worker thread and ``ready`` flips to True when they are available.
Until then ``paint()`` returns False and ``HudCanvas`` keeps drawing the
wireframe, so the panel is never empty and the swap is seamless.

Why QImage and not QMovie
-------------------------
QMovie drives itself off Qt's clock; it cannot be paused, re-timed or re-seeked
per audio frame, which is exactly what lip-sync needs. Decoding to a list of
QImages lets ``step()`` choose the frame from the live audio level instead.

Only Pillow, PyQt6 and the existing asset vocabulary are required. Everything is
best-effort: any failure leaves ``ready`` False and the wireframe in place.
"""
from __future__ import annotations

import bisect
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter

try:  # pragma: no cover - environment dependent
    from PIL import Image
    _PIL_OK = True
    _PIL_ERR: Optional[BaseException] = None
except Exception as _e:  # pragma: no cover - environment dependent
    Image = None  # type: ignore
    _PIL_OK = False
    _PIL_ERR = _e

# Reuse the exact asset vocabulary the rest of the avatar stack uses, so a
# dropped-in sara_avatar.gif is honoured here without any second list to keep in
# sync. gui.avatar guards its tkinter import, so importing it is safe even in a
# PyQt-only process (no Tk root is ever created).
try:  # pragma: no cover - import wiring
    from gui.avatar import (
        DEFAULT_GIF_NAME, STATE_GIF_CANDIDATES, STATE_COLORS,
        IDLE, LISTENING, PROCESSING, RESEARCHING, WRITING, SPEAKING, STATES,
        normalize_state, resolve_state_gifs, find_primary_gif,
        resolve_gesture_gifs, default_base_dir,
    )
    _VOCAB_OK = True
    _VOCAB_ERR: Optional[BaseException] = None
except Exception as _e:  # pragma: no cover - defensive
    _VOCAB_OK = False
    _VOCAB_ERR = _e

    # Minimal fallback so a broken gui.avatar still leaves a working avatar.
    DEFAULT_GIF_NAME = "sara_avatar.gif"
    HUD_BG = "#00060a"
    IDLE, LISTENING, PROCESSING, RESEARCHING, WRITING, SPEAKING = (
        "Idle", "Listening", "Processing", "Researching", "Writing", "Speaking",
    )
    STATES = (IDLE, LISTENING, PROCESSING, RESEARCHING, WRITING, SPEAKING)
    STATE_COLORS = {
        IDLE: "#00d4ff", LISTENING: "#00ff88", PROCESSING: "#7aa2ff",
        RESEARCHING: "#b07aff", WRITING: "#ffcc00", SPEAKING: "#ff6b00",
    }
    STATE_GIF_CANDIDATES = {
        IDLE: (DEFAULT_GIF_NAME, "sara_idle.gif", "idle.gif", "Just wating.gif", "widget.gif"),
        LISTENING: ("sara_listening.gif", DEFAULT_GIF_NAME, "sara_idle.gif", "idle.gif", "Just wating.gif", "widget.gif"),
        PROCESSING: ("sara_thinking.gif", DEFAULT_GIF_NAME, "thinking.gif", "idle.gif"),
        RESEARCHING: ("sara_thinking.gif", "sara_researching.gif", DEFAULT_GIF_NAME, "thinking.gif", "idle.gif"),
        WRITING: ("sara_writing.gif", "sara_thinking.gif", DEFAULT_GIF_NAME, "thinking.gif", "idle.gif"),
        SPEAKING: ("sara_speaking.gif", "sara_talking.gif", DEFAULT_GIF_NAME, "talking.gif", "idle.gif"),
    }
    _STATE_ALIASES = {
        "INITIALISING": PROCESSING, "INITIALIZING": PROCESSING,
        "THINKING": PROCESSING, "SLEEPING": IDLE, "ASLEEP": IDLE,
        "MUTED": IDLE, "RESEARCH": RESEARCHING, "WRITE": WRITING,
    }

    def normalize_state(state: str) -> str:  # type: ignore[misc]
        if not state:
            return IDLE
        s = str(state).strip()
        if s in STATES:
            return s
        return _STATE_ALIASES.get(s.upper(), IDLE)

    def default_base_dir() -> Path:  # type: ignore[misc]
        return Path(__file__).resolve().parent.parent

    def resolve_state_gifs(base_dir=None):  # type: ignore[misc]
        return {s: None for s in STATES}

    def resolve_gesture_gifs(base_dir=None):  # type: ignore[misc]
        return {"wave": None}

    def find_primary_gif(base_dir=None):  # type: ignore[misc]
        return None


_RESAMPLE = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None) \
    if _PIL_OK else None


class GifClip:
    """One decoded GIF: RGBA frames plus per-frame durations.

    Frames are pre-scaled on the worker thread so ``paint()`` never resizes on
    the hot path, and a cumulative table makes frame lookup for a given time an
    O(log n) bisect instead of a walk.
    """

    __slots__ = ("path", "frames", "durations", "total", "_cum")

    def __init__(self, path: Path, max_dim: int = 0) -> None:
        self.path = Path(path)
        self.frames: list[QImage] = []
        self.durations: list[float] = []
        self._cum: list[float] = []
        self._load(max_dim)

    def _load(self, max_dim: int) -> None:
        with Image.open(self.path) as im:
            n = int(getattr(im, "n_frames", 1) or 1)
            for i in range(n):
                try:
                    im.seek(i)
                except EOFError:
                    break
                try:
                    dur = float(im.info.get("duration", 100) or 100)
                except (TypeError, ValueError):
                    dur = 100.0
                self.durations.append(max(0.02, dur / 1000.0))

                frame = im.convert("RGBA")
                if max_dim and max(frame.size) > max_dim:
                    r = max_dim / float(max(frame.size))
                    size = (max(1, int(frame.width * r)),
                            max(1, int(frame.height * r)))
                    frame = frame.resize(size, _RESAMPLE)
                data = frame.tobytes("raw", "RGBA")
                # copy() is essential: QImage does not own `data`, which dies at
                # the end of this loop iteration.
                self.frames.append(
                    QImage(data, frame.width, frame.height,
                           frame.width * 4,
                           QImage.Format.Format_RGBA8888).copy())

        acc = 0.0
        for d in self.durations:
            acc += d
            self._cum.append(acc)
        self.total = acc

    def frame_at(self, t: float) -> int:
        if not self.frames:
            return 0
        if self.total <= 0:
            return 0
        t = t % self.total
        i = bisect.bisect_right(self._cum, t)
        if i >= len(self.frames):
            return len(self.frames) - 1
        return i


class QtGifAvatar:
    """Animated GIF avatar for HudCanvas.

    Thread model: ``__init__`` starts one daemon worker that decodes every
    distinct clip. Everything after that (``step``, ``paint``) is called on the
    Qt thread only. A lock guards the clip table and the animation cursor.
    """

    def __init__(self, base_dir: Optional[str] = None,
                 max_dim: Optional[int] = None,
                 autoload: bool = True) -> None:
        self.base_dir = Path(base_dir) if base_dir else default_base_dir()
        if max_dim is None:
            try:
                max_dim = int(os.environ.get("ZARA_GIF_MAX", "480"))
            except Exception:
                max_dim = 480
        self.max_dim = max(120, int(max_dim))

        self.ready = False
        self.status = "init"
        self.error: Optional[BaseException] = None

        self._lock = threading.RLock()
        self._clips: dict[str, GifClip] = {}
        self._state = IDLE
        self._speaking = False
        self._amp = 0.0
        self._t = 0.0
        self._idx = 0
        self._key: Optional[str] = None
        self._clip: Optional[GifClip] = None

        # Transition smoothing: a short dwell suppresses state flicker (rapid
        # Processing/Researching/Writing toggles) and the last frame of the old
        # clip is crossfaded out so a switch does not read as a hard cut.
        self._pending_state = IDLE
        self._pending_since = 0.0
        self._switch_dwell = 0.16
        self._prev_img: Optional[QImage] = None
        self._fade = 0.0
        self._fade_dur = 0.18

        # Transient gesture overlay (a wave on hello / bye). It plays once on
        # top of whatever the avatar is doing, independently of the state
        # machine, then removes itself. Absent clip -> the gesture is a no-op.
        self._gestures: dict[str, GifClip] = {}
        self._gesture: Optional[GifClip] = None
        self._gesture_key: Optional[str] = None
        self._gesture_t = 0.0
        self._gesture_loops = 1
        self._gesture_active = False
        self._pending_gesture: Optional[tuple[str, int]] = None

        try:
            self._paths = resolve_state_gifs(self.base_dir) if _VOCAB_OK \
                else {s: None for s in STATES}
        except Exception:
            self._paths = {s: None for s in STATES}
        try:
            self._primary = find_primary_gif(self.base_dir) if _VOCAB_OK else None
        except Exception:
            self._primary = None

        if autoload:
            threading.Thread(target=self._load_worker, name="zara-gif-avatar",
                             daemon=True).start()

    # -- loading -------------------------------------------------------------
    def _load_worker(self) -> None:
        if not _PIL_OK:
            with self._lock:
                self.status = f"no Pillow: {_PIL_ERR}"
            return
        wanted: dict[str, Path] = {}
        # Primary first so the panel can swap from wireframe to GIF as soon as
        # the first clip is decoded, instead of waiting for the whole set.
        for p in [self._primary] + list(getattr(self, "_paths", {}).values()):
            if p is not None:
                wanted.setdefault(str(p), Path(p))

        clips: dict[str, GifClip] = {}
        notes: list[str] = []

        # Gesture clips first: they are small and the startup greeting can fire
        # within a fraction of a second, so they must be ready before the big
        # character clips (which take ~2 s for 50 frames each).
        gestures: dict[str, GifClip] = {}
        try:
            gpaths = resolve_gesture_gifs(self.base_dir) if _VOCAB_OK else {}
        except Exception:
            gpaths = {}
        for gname, gpath in (gpaths or {}).items():
            if gpath is None:
                continue
            try:
                c = GifClip(gpath, max_dim=self.max_dim)
                if c.frames:
                    gestures[gname] = c
                    notes.append(f"{gpath.name} {len(c.frames)}f (gesture)")
            except Exception as e:
                notes.append(f"{gpath.name} gesture failed: {e}")
        with self._lock:
            self._gestures = gestures

        for key, path in wanted.items():
            try:
                c = GifClip(path, max_dim=self.max_dim)
                if c.frames:
                    clips[key] = c
                    notes.append(f"{path.name} {len(c.frames)}f")
                else:
                    notes.append(f"{path.name} empty")
            except Exception as e:  # one bad file must not kill the rest
                notes.append(f"{path.name} failed: {e}")
                continue

            with self._lock:
                if key in clips:
                    # Publish progressively; the first successful clip flips
                    # `ready` so HudCanvas can start drawing the GIF at once.
                    self._clips = dict(clips)
                    if not self.ready:
                        self._select_locked()
                        self._t = 0.0
                        self._idx = 0
                        self.ready = True
                    self.status = "gif: " + ", ".join(notes)

        with self._lock:
            if not self.ready:
                self.ready = False
                self.status = "no gif (" + "; ".join(notes) + ")"
            if not _PIL_OK:
                self.error = _PIL_ERR
            # A gesture requested before its clip finished decoding is honoured
            # now, so an early greeting wave is not silently dropped.
            if self._pending_gesture is not None:
                pname, ploops = self._pending_gesture
                pclip = self._gestures.get(pname)
                if pclip is not None and pclip.frames:
                    self._gesture = pclip
                    self._gesture_key = pname
                    self._gesture_t = 0.0
                    self._gesture_loops = max(1, ploops)
                    self._gesture_active = True
                    self._pending_gesture = None

    def _select_locked(self) -> None:
        path = self._paths.get(self._state) or self._primary
        if path is None:
            self._clip = None
            self._key = None
            return
        key = str(path)
        clip = self._clips.get(key)
        # If a state resolves to a clip we have not decoded, keep the previous
        # clip rather than going blank; the default build always preloads all.
        if clip is None:
            return
        if key != self._key or clip is not self._clip:
            self._clip = clip
            self._key = key

    def _switch_locked(self, target: str) -> None:
        """Move to `target`, snapshotting the current frame for a crossfade."""
        if target == self._state:
            return
        frames = self._clip.frames if self._clip is not None else None
        if frames and 0 <= self._idx < len(frames):
            self._prev_img = frames[self._idx]
            self._fade = self._fade_dur
        self._state = target
        self._select_locked()
        self._t = 0.0
        self._idx = 0

    def play_gesture(self, name: str = "wave", loops: int = 1) -> bool:
        """Start a one-shot gesture overlay (e.g. a wave). Qt thread only.

        Returns True when a matching clip exists and playback started, False
        when the gesture is unavailable (missing clip) — callers can ignore it.
        """
        key = str(name or "wave").strip().lower()
        try:
            loops = max(1, int(loops))
        except (TypeError, ValueError):
            loops = 1
        with self._lock:
            clip = self._gestures.get(key)
            if clip is None or not clip.frames:
                # Not decoded yet — remember it so the loader can start it.
                self._pending_gesture = (key, loops)
                return False
            self._pending_gesture = None
            self._gesture = clip
            self._gesture_key = key
            self._gesture_t = 0.0
            self._gesture_loops = loops
            self._gesture_active = True
        return True

    def has_gesture(self, name: str = "wave") -> bool:
        with self._lock:
            clip = self._gestures.get(str(name or "wave").strip().lower())
            return bool(clip is not None and clip.frames)

    # -- animation -----------------------------------------------------------
    def step(self, dt: float, amp: float, speaking: bool, muted: bool,
             state: str, v_open: Optional[float] = None,
             v_wide: float = 0.0, v_level: Optional[float] = None) -> None:
        """Advance the animation. Called on the Qt thread from HudCanvas._step.

        ``amp`` is the smoothed 0..1 audio level. While speaking it drives the
        playback rate — louder speech advances the talking clip faster and quiet
        gaps nearly stall it — and on the rising edge of speech the clip restarts
        from its first frame so the mouth opens with the first word instead of
        wherever the idle loop happened to be.
        """
        with self._lock:
            if not self.ready or self._clip is None:
                return
            try:
                dt = max(0.0, min(float(dt), 0.25))
            except (TypeError, ValueError):
                dt = 0.0
            try:
                amp = max(0.0, min(1.0, float(amp)))
            except (TypeError, ValueError):
                amp = 0.0
            try:
                openness = max(0.0, min(1.0, float(v_open))) \
                    if v_open is not None else 0.0
            except (TypeError, ValueError):
                openness = 0.0

            now = time.monotonic()
            target = IDLE if muted else normalize_state(state)
            if target == self._state:
                self._pending_state = target
                self._pending_since = now
            else:
                # Speech and mute are timing-critical — the mouth must match the
                # voice at once. Anything else is debounced so a burst of
                # state churn cannot strobe the GIF.
                immediate = (muted or target == SPEAKING
                             or self._state == SPEAKING)
                if immediate:
                    self._pending_state = target
                    self._pending_since = now
                    self._switch_locked(target)
                elif target != self._pending_state:
                    self._pending_state = target
                    self._pending_since = now
                elif now - self._pending_since >= self._switch_dwell:
                    self._switch_locked(target)

            started = speaking and not self._speaking
            self._speaking = bool(speaking)
            self._amp = amp
            if started:
                self._t = 0.0
                self._idx = 0

            if self._fade > 0.0:
                self._fade = max(0.0, self._fade - dt)
                if self._fade == 0.0:
                    self._prev_img = None

            if self._gesture_active and self._gesture is not None:
                self._gesture_t += dt
                _gtotal = self._gesture.total * max(1, self._gesture_loops)
                if _gtotal <= 0 or self._gesture_t >= _gtotal:
                    self._gesture_active = False
                    self._gesture = None
                    self._gesture_key = None

            clip = self._clip
            if clip is None or not clip.frames:
                return

            if self._speaking:
                # Speech energy -> cadence. A floor keeps a faint idle motion in
                # the quietest gaps so the face never freezes mid-sentence.
                speed = 0.5 + 2.6 * max(amp, openness)
            else:
                speed = 1.0 + 0.35 * amp
            self._t += dt * speed
            if clip.total > 0:
                self._t %= clip.total
            self._idx = clip.frame_at(self._t)

    # -- drawing -------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def amplitude(self) -> float:
        return self._amp

    def describe(self) -> str:
        return self.status

    def _draw_fitted(self, painter: QPainter, img: QImage, rect: QRectF,
                     opacity: float = 1.0, speaking: bool = False,
                     amp: float = 0.0) -> None:
        """Draw `img` letterboxed in `rect`, with optional talk breathing."""
        iw, ih = img.width(), img.height()
        if iw <= 0 or ih <= 0:
            return
        rw, rh = float(rect.width()), float(rect.height())
        if rw <= 0 or rh <= 0:
            return
        scale = min(rw / iw, rh / ih)
        w, h = iw * scale, ih * scale
        x = rect.x() + (rw - w) / 2.0
        y = rect.y() + (rh - h) / 2.0
        # A whisper of amplitude-driven "breathing" so the still frames do not
        # read as a sticker when Zara is talking.
        if speaking and amp > 0.02:
            grow = 1.0 + 0.018 * amp
            gw, gh = w * grow, h * grow
            x -= (gw - w) / 2.0
            y -= (gh - h) / 2.0
            w, h = gw, gh
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.drawImage(QRectF(x, y, w, h), img)
        painter.setOpacity(1.0)

    def paint(self, painter: QPainter, rect: QRectF) -> bool:
        """Draw the current frame centred inside ``rect``. True if drawn."""
        with self._lock:
            clip = self._clip
            idx = self._idx
            prev = self._prev_img
            fade = self._fade
            fade_dur = self._fade_dur
            speaking = self._speaking
            amp = self._amp
            gest = self._gesture if self._gesture_active else None
            gest_t = self._gesture_t
            gest_loops = self._gesture_loops
        if clip is None or not clip.frames:
            return False
        frames = clip.frames
        if idx < 0 or idx >= len(frames):
            idx = 0
        img = frames[idx]
        if img.width() <= 0 or img.height() <= 0:
            return False

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        alpha = 1.0
        if prev is not None and fade > 0.0 and fade_dur > 0.0:
            # Dissolve from the last frame of the outgoing clip to the live
            # first frames of the incoming one, so a state change is a fade,
            # not a cut.
            alpha = max(0.0, min(1.0, 1.0 - fade / fade_dur))
            self._draw_fitted(painter, prev, rect, 1.0)
        self._draw_fitted(painter, img, rect, alpha,
                          speaking=speaking, amp=amp)
        # Gesture overlay on top of the avatar: a hand waving hello/bye. Drawn
        # full-band and alpha-composited, so a translucent hand clip (the
        # default) waves over the character while an opaque full-frame clip
        # (a supplied "waving character") simply covers it for the gesture.
        if gest is not None and gest.frames:
            try:
                total = gest.total * max(1, gest_loops)
                gf = gest.frames[gest.frame_at(gest_t)]
                ga = 1.0
                if gest_t < 0.15:
                    ga = max(0.0, min(1.0, gest_t / 0.15))
                tail = total - gest_t
                if tail < 0.15:
                    ga = min(ga, max(0.0, tail / 0.15))
                if ga > 0.0 and gf.width() > 0 and gf.height() > 0:
                    self._draw_fitted(painter, gf, rect, ga)
            except Exception:
                pass
        painter.restore()
        return True


__all__ = [
    "QtGifAvatar", "GifClip", "normalize_state", "resolve_state_gifs",
    "find_primary_gif", "STATE_COLORS", "STATES", "DEFAULT_GIF_NAME",
    "IDLE", "LISTENING", "PROCESSING", "RESEARCHING", "WRITING", "SPEAKING",
]
