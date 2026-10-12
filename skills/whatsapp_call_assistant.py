"""Zara answers WhatsApp calls on Rustam's behalf, and says why.

WHAT THIS IS
    A background listener that notices an incoming call in WhatsApp Desktop,
    accepts it, tells the caller that the owner is busy, waits for them to speak,
    says goodbye, and hangs up. It runs for the whole lifetime of the app on its
    own daemon thread, started from main.py.

THE ONE THING THAT HAS TO BE TRUE FOR THE CALLER TO HEAR ANYTHING
    Everything Zara says is played to a *speakers* endpoint. WhatsApp sends its
    call audio into whatever Windows has configured as its microphone. Those two
    are different endpoints, so for a caller to hear the greeting the machine has
    to be bridged — the standard arrangement is a virtual cable (VB-CABLE,
    VoiceMeeter):

        WhatsApp  --microphone-->  "CABLE Output" (recording side)
        Zara      --speakers-->    "CABLE Input"  (playback side, the bridge)

    pyttsx3 and core.tts both play on the system default output device, so this
    module checks that endpoint once and says so in the log when it is a real
    speaker rather than a cable. That is the single most common reason "Zara
    answered but the caller heard nothing", and nothing inside the app can
    diagnose it: the write succeeded, the playback succeeded, and the sound went
    to a speaker in another room.

    `call_audio_device` overrides the default. Give it the name of a playback
    device as listed by ⚙ → Audio and that one is used instead, resolved through
    core.audio_devices so it goes through the same host-API negotiation as
    everything else in this project.

AVATAR
    The GIF avatar is driven over the same vocabulary as the live session
    (gui/avatar.py's states), because the voice a caller hears and the face on
    screen have to agree about who is talking:

        In Call     the call is up, before anything has been said
        Speaking    Zara is talking to the caller
        Listening   the caller is talking, or Zara is waiting for them to
        Idle        the call is over

    It arrives as `on_state`, so this module never imports main and never
    touches Qt. The listener thread calls it directly; emit_avatar_state is
    documented as thread-safe and never raising.

THREADING CONTRACT
    The listener thread never raises into its caller, never touches Qt, and
    stops promptly after stop(). Every callback it invokes runs on that thread,
    so anything passed in must be safe to call from off the Qt thread —
    `ui.write_log` is, and that is why it is the one that is passed.

    Speech is serialised behind one lock inside the engine. Two overlapping
    greetings from two threads is the failure mode where Zara talks over Zara,
    and a lock is cheaper than a queue that only ever holds one item.

WHY A POLL AND NOT AN EVENT
    A WhatsApp call is not an event anything publishes. The only signal is "a
    control with this text exists in this window right now", so the loop has to
    exist however the window is found. At the default 1.5 s interval a call is
    picked up well inside WhatsApp's own ~30 s ring timeout, and the scan is
    bounded (see _scan) so it cannot turn into a full accessibility walk of a
    large Electron tree once a second.
"""

from __future__ import annotations

import os
import threading
import time

# ── Avatar states ─────────────────────────────────────────────────────────────
# gui/avatar.py's own strings, spelled out here so this module never has to
# import the avatar (which would drag in Pillow and tkinter) just to name a
# state. Normalisation on the bus side maps anything unrecognised to Idle, so a
# rename here degrades to a still face rather than an exception.
STATE_IN_CALL   = "In Call"
STATE_SPEAKING  = "Speaking"
STATE_LISTENING = "Listening"
STATE_IDLE      = "Idle"

# ── Button vocabulary ─────────────────────────────────────────────────────────
# Matched case-insensitively after normalisation (see _norm), and these strings
# are *localised*. The labels below are the ones this build was written against;
# a miss costs a call that goes unanswered, not a crash — which is why they live
# in one block at the top of the file instead of being scattered through the
# matching logic. A WhatsApp set to a language not listed here simply finds
# nothing, and the module says so once rather than failing silently forever.
ANSWER_LABELS = frozenset({
    "accept", "accept call", "answer", "answer call",
    "kabul", "kabul et", "kabul et\u00e7a\u011fr\u0131y\u0131",
    "yan\u0131tla", "cevapla", "cevap ver", "aramay\u0131 kabul et",
})
# No bare "end". Matched as a substring it also catches "Send message" and
# "weekend", and a wrong click here hangs up on a live conversation. "End call"
# is what WhatsApp actually labels it.
DECLINE_LABELS = frozenset({
    "decline", "decline call", "reject", "reject call",
    "end call", "hang up",
    "reddet", "reddet \u00e7a\u011fr\u0131y\u0131",
    "\u00e7a\u011fr\u0131y\u0131 sonland\u0131r", "aramay\u0131 sonland\u0131r",
    "sonland\u0131r", "bitir",
})
# Soft signal only, used for logging. WhatsApp surfaces an incoming call with
# exactly one Accept control, so it is that control's absence — not the presence
# of these words — that proves nothing is ringing. A localised build
# translating none of them must still be answerable.
RINGING_HINTS = (
    "ringing", "incoming call", "incoming voice call",
    "aran\u0131yor", "gelen \u00e7a\u011fr\u0131", "gelen arama",
)

# Control types Chromium's accessibility tree puts a clickable label under. It
# exposes these as Button or Text depending on the element, and WhatsApp has
# used both across versions, so both are searched — in that order.
_CONTROL_TYPES = ("Button", "Text")

# Labels that read like a call control but must never be clicked.
_NEVER_CLICK = (
    "cancel", "add call", "call back", "mute", "speaker", "video",
    "message", "reply", "history", "close", "invite", "group",
)

# Required on the substring path, and the reason that path is safe. A loose
# match is only accepted when the label also says what it is about — "Accept
# call", "Answer the call", "Kabul et (arama)" — which is exactly the shape a
# localised, unlisted call button takes. It is what keeps a chat-list row called
# "Accept my invite" from being clicked in the middle of answering: it mentions
# an invite, not a call. Without this the fallback that exists to cover an
# unlisted language is also the thing that misclicks.
_CALL_CONTEXT = ("call", "arama", "\u00e7a\u011fr\u0131", "ring")

# ── Timings ───────────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL = 1.5     # s between scans for an incoming call
CONNECT_TIMEOUT       = 2.0     # s pywinauto may spend attaching to WhatsApp
CONNECT_RETRY         = 30.0    # s between reconnection attempts while it is gone
CALL_SETTLE           = 1.2     # s after answering, for the call audio to route
SILENCE_TIMEOUT       = 45.0    # s of no caller speech before giving up politely
CALLER_GRACE          = 12.0    # s of quiet after the caller stops speaking
MAX_CALL_SECONDS      = 120.0   # s hard ceiling, enforced before the greeting

# Caller-speech detection, same shape as main.py's own level mapping: a floor
# and a full-scale value. The floor is the one that matters — get it wrong in
# the permissive direction and the line's own hiss counts as the caller talking,
# which hangs up on them mid-sentence.
_VOICE_FLOOR = 500.0
_VOICE_FULL  = 3200.0
_VOICE_HANG  = 0.8           # s a level above the floor must hold before it counts
_VOICE_GATE  = _VOICE_FLOOR / _VOICE_FULL
_VOICE_RATE  = 16000
_VOICE_POLL  = 0.4           # s between level readings while on a call

# Bounds on the accessibility scan. WhatsApp Desktop is Chromium, so its tree is
# large; walking all of it once a second would burn a core for nothing. The call
# overlay sits at the top of the tree — it is drawn over the chat list, not
# inside it — so depth comes first and element count second, and both are hard
# caps rather than hints.
_SCAN_DEPTH     = 8
_SCAN_MAX_NODES = 400

# Two matches for "WhatsApp", tried in order: the window itself first (the cheap
# and usual case), then any title mentioning it, because the in-call window is
# titled differently on some builds and that is exactly the window that has to
# be reachable.
_WINDOW_TITLES = (
    r"(?i)^\s*whatsapp(?:\s*[-.\u2013\u2014].*)?$",
    r"(?i)whatsapp",
)


def _norm(text: str) -> str:
    """Lower-case, collapse whitespace, strip trailing punctuation and glyphs.

    Windows folds several glyphs into a control's accessible name — the Accept
    button regularly arrives as "Accept  \u2192" or "Accept..." — and an exact
    comparison that ignored that would find nothing.
    """
    t = " ".join(str(text or "").split()).lower()
    return t.strip(" \u2022\u2192\u25b6\u25b8\u00bb\u00ab\u2026.!?-|")


class _Label:
    """A clickable control found in WhatsApp's tree."""

    __slots__ = ("ctrl", "label", "rect")

    def __init__(self, ctrl, label: str, rect):
        self.ctrl  = ctrl
        self.label = label
        self.rect  = rect

    def midpoint(self):
        r = self.rect
        return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)

    def __repr__(self) -> str:                     # pragma: no cover - debug aid
        return f"<CallLabel {self.label!r}>"


# ── The voice ─────────────────────────────────────────────────────────────────

class Pyttsx3Voice:
    """SAPI5 through pyttsx3 — offline, no network, no API key.

    Held for the life of the listener rather than rebuilt per call: init()
    enumerates installed voices and can take a second, and paying that on the
    first incoming call means answering in silence for a moment.

    pyttsx3 is not thread-safe on its own, so the lock lives here instead of at
    every call site.
    """

    def __init__(self, rate: int = 175, volume: float = 1.0):
        import pyttsx3                                  # noqa: PLC0415 - lazy

        self._engine = pyttsx3.init()
        self._lock   = threading.Lock()
        for prop, value in (("rate", int(rate)), ("volume", float(volume))):
            try:
                self._engine.setProperty(prop, value)
            except Exception:
                pass                                    # a voice may not have one

    def speak(self, text: str) -> None:
        with self._lock:
            self._engine.say(text)
            self._engine.runAndWait()

    def stop(self) -> None:
        # Deliberately outside the lock: stop() unblocks runAndWait() from
        # another thread, and holding the lock here would deadlock against the
        # very speak() it is meant to cut off.
        try:
            self._engine.stop()
        except Exception:
            pass


class _AppVoice:
    """Zara's own offline voice (core.tts), as a drop-in for Pyttsx3Voice.

    The fallback when pyttsx3 is not installed, and not a downgrade — it is the
    voice the settings panel already previews with. core.tts emits
    "Speaking"/"Idle" through the process-wide voice-state hook itself, which is
    what keeps the avatar in step on that path without any code here.
    """

    def __init__(self):
        import json                                     # noqa: PLC0415
        from pathlib import Path

        from core.tts import create_tts_player           # noqa: PLC0415

        cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError):
            config = {}

        self._player = create_tts_player(config)

    def speak(self, text: str) -> None:
        self._player.speak(text)

    def stop(self) -> None:
        try:
            self._player.stop()
        except Exception:
            pass


class _NullVoice:
    """For when there is no way to speak at all.

    The call is still answered and still ended. Refusing to answer because the
    speech engine is missing would leave the caller on a dead line, which is
    worse in every way than a call that was answered silently.
    """

    def speak(self, text: str) -> None:
        print(f"[CallAssistant] NO SPEECH ENGINE — the caller did not hear: "
              f"{text}")

    def stop(self) -> None:
        pass


class _CallableVoice:
    """Wraps a plain speak(text) callable in the engine interface."""

    def __init__(self, fn):
        self._fn = fn

    def speak(self, text: str) -> None:
        self._fn(text)

    def stop(self) -> None:
        pass


# ── Caller audio ──────────────────────────────────────────────────────────────

class CallerAudioMonitor:
    """Loudness on the call's own microphone endpoint, for "did they speak?".

    This is the honest way to answer requirement 3 on this platform. WhatsApp
    Desktop exposes no call object, no transcript and no speech event — the only
    evidence a caller said anything is the sound of their voice arriving on the
    machine. Measuring RMS on the input endpoint the call is already using is
    that measurement, and it costs one stream.

    Best-effort by construction: if the endpoint cannot be opened (no cable
    installed, endpoint already busy with the live session) the monitor reports
    silence, the call runs to SILENCE_TIMEOUT, and the farewell still lands. The
    timeout path is the default path, not the failure path.
    """

    def __init__(self, device=None, rate: int = _VOICE_RATE):
        self.device = device
        self.rate   = int(rate)
        self._level = 0.0
        self._voice_since = 0.0     # monotonic time the level first crossed the gate
        self._stream = None
        self._lock  = threading.Lock()

    # -- lifecycle --

    def start(self) -> bool:
        """Open the input stream. False means "monitor unavailable"."""
        if self._stream is not None:
            return True
        try:
            import numpy as np                          # noqa: PLC0415
            import sounddevice as sd                    # noqa: PLC0415

            def _cb(indata, frames, t, status):         # noqa: ARG001
                # Overflow is not fatal here: this is a level meter, and a
                # dropped buffer costs one reading, not the call.
                if status:
                    return
                x = np.asarray(indata, dtype=np.float32)
                if x.size:
                    rms = float(np.sqrt(np.mean(x * x)))
                    with self._lock:
                        self._level = min(1.0, rms / _VOICE_FULL)

            self._stream = sd.InputStream(
                samplerate=self.rate, channels=1, dtype="int16",
                device=self.device, blocksize=1024, callback=_cb)
            self._stream.start()
            return True
        except Exception as e:
            print(f"[CallAssistant] caller-audio monitor unavailable: {e}")
            self._stream = None
            return False

    def stop(self) -> None:
        st, self._stream = self._stream, None
        if st is None:
            return
        for step in (getattr(st, "stop", None), getattr(st, "close", None)):
            try:
                if step is not None:
                    step()
            except Exception:
                pass

    # -- readings --

    def level(self) -> float:
        with self._lock:
            return self._level

    def heard_voice(self, now: float | None = None) -> bool:
        """True once a level above the gate has held for _VOICE_HANG.

        Not pure: it latches the moment the level crossed the gate, which is
        state that has to live somewhere and this thread is the only reader.
        """
        now = time.monotonic() if now is None else now
        if self.level() >= _VOICE_GATE:
            if not self._voice_since:
                self._voice_since = now
            return (now - self._voice_since) >= _VOICE_HANG
        self._voice_since = 0.0
        return False


# ── The skill ─────────────────────────────────────────────────────────────────

class ZaraWhatsAppCallAssistant:
    """Detects WhatsApp Desktop calls and answers them on the owner's behalf.

    `speak` / `on_state` / `log` / `on_event` are injected, so this module never
    imports main, never reaches Qt, and learns the owner's name only from its own
    arguments.
    """

    def __init__(
        self,
        speak=None,
        on_state=None,
        log=None,
        on_event=None,
        owner_name: str = "Rustam Ali",
        assistant_name: str = "Zara",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        silence_timeout: float = SILENCE_TIMEOUT,
        caller_grace: float = CALLER_GRACE,
        max_call_seconds: float = MAX_CALL_SECONDS,
        call_audio_device: str | None = None,
        wait_for_caller: bool = True,
        enabled: bool = True,
    ):
        self.owner_name       = owner_name
        self.assistant_name   = assistant_name
        self.poll_interval    = float(poll_interval)
        self.silence_timeout  = float(silence_timeout)
        self.caller_grace     = float(caller_grace)
        # A ceiling of zero or less would mean "hang up without speaking", which
        # is a plausible thing to ask for and never what anyone means. Clamped to
        # the default rather than rejected, so a bad value from a config file
        # cannot stop WhatsApp being answered at all.
        self.max_call_seconds = (float(max_call_seconds) if max_call_seconds > 0
                                 else float(MAX_CALL_SECONDS))

        self._on_state  = on_state or (lambda *_a, **_kw: None)
        self._log       = log or (lambda msg: print(msg))
        self._on_event  = on_event or (lambda *_a, **_kw: None)
        self._speak_cb  = speak

        self._enabled    = bool(enabled)
        self._voice      = None        # resolved lazily, on the first call
        self._voice_kind = ""

        self._call_device_name = call_audio_device
        self._call_device_idx  = None      # resolved on the listener thread
        self._wait_for_caller  = bool(wait_for_caller)
        self._warned_route     = False
        self._monitor: CallerAudioMonitor | None = None

        self._app     = None
        self._in_call = False
        self._retry_at = 0.0
        self._last_hangup_check = 0.0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._call_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Launch the detection loop. Idempotent. False if disabled."""
        if not self._enabled:
            self._log("SYS: WhatsApp call answering is off "
                      "(ZARA_WHATSAPP_CALL_ASSISTANT=0).")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="zara-whatsapp-call-listener", daemon=True)
        self._thread.start()
        self._log(f"SYS: {self.assistant_name} will answer WhatsApp calls "
                  f"on {self.owner_name}'s behalf.")
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop, and hang up on any call still in progress."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        try:
            if self._in_call:
                self.end_call(reason="listener stopped")
        except Exception as e:
            print(f"[CallAssistant] stop(): {e}")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def in_call(self) -> bool:
        return self._in_call

    # ── the loop ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:                  # never let the thread die
                print(f"[CallAssistant] poll failed: {type(e).__name__}: {e}")
            self._stop.wait(self.poll_interval)

    def poll_once(self) -> bool:
        """One scan for an incoming call. True if a call was answered.

        Public so it can be driven by hand — by a test, or to check that
        WhatsApp is being seen at all — without starting a thread.
        """
        if not self._enabled or self._in_call:
            return False
        if time.monotonic() < self._retry_at:
            return False

        if self._app is None and not self._connect():
            self._retry_at = time.monotonic() + CONNECT_RETRY
            return False

        win = self._whatsapp_window()
        if win is None:
            # WhatsApp closed, or the handle we attached to is gone. Dropping
            # it is what lets _connect() try again; keeping it would leave the
            # loop scanning a dead object forever and never come back.
            self._app = None
            self._retry_at = time.monotonic() + CONNECT_RETRY
            return False

        nodes = list(self._scan(win))
        answer = self._pick(nodes, ANSWER_LABELS)
        if answer is None:
            return False

        if self._is_ringing(nodes):
            self._on_event("ringing", {})
        self._log("SYS: Incoming WhatsApp call — answering.")
        return self._take_call(answer)

    # ── attaching to WhatsApp ────────────────────────────────────────────────

    def _connect(self) -> bool:
        """Attach pywinauto to the running WhatsApp Desktop window."""
        try:
            from pywinauto.application import Application    # noqa: PLC0415
        except ImportError:
            # A declared dependency, so this means a broken install rather than
            # a missing one — and retrying every 30 s would only print the same
            # line for the life of the process.
            self._log("ERR: pywinauto is not installed — WhatsApp call "
                      "answering is unavailable. pip install pywinauto")
            self._enabled = False
            return False

        for title in _WINDOW_TITLES:
            try:
                self._app = Application(backend="uia").connect(
                    title_re=title, timeout=CONNECT_TIMEOUT)
                return True
            except Exception:
                continue
        return False

    def _whatsapp_window(self):
        """The top-level WhatsApp window, or None if it has gone away."""
        app = self._app
        if app is None:
            return None
        try:
            for spec in ({"title_re": t} for t in _WINDOW_TITLES):
                try:
                    win = app.window(**spec)
                    if win.exists(timeout=0.2):
                        return win
                except Exception:
                    continue
            # No titled window matched. Fall back to the process's visible
            # top-level windows before giving up: on several WhatsApp versions
            # the call overlay is its own window with its own title.
            for win in app.windows():
                try:
                    if win.is_visible():
                        return win
                except Exception:
                    continue
        except Exception:
            self._app = None
        return None

    # ── the accessibility scan ───────────────────────────────────────────────

    def _scan(self, win):
        """Yield (control, normalised name) for named elements in the tree.

        Breadth-first, depth-capped and node-capped. See _SCAN_DEPTH for why a
        full walk is not an option on a Chromium app polled once a second.
        """
        frontier  = [win]
        seen_nodes = 0
        for _ in range(_SCAN_DEPTH):
            nxt = []
            for parent in frontier:
                try:
                    children = parent.children()
                except Exception:
                    continue
                for child in children:
                    seen_nodes += 1
                    try:
                        name = child.window_text()
                    except Exception:
                        name = None
                    if name:
                        yield child, _norm(name)
                    # Descended into whether or not this element had a name.
                    # A nameless element is usually a *container* — that is how
                    # Chromium groups the call overlay — so skipping its subtree
                    # on a failed name read hides the whole thing behind the
                    # first unnamed pane.
                    nxt.append(child)
                    if seen_nodes >= _SCAN_MAX_NODES:
                        return
            frontier = nxt
            if not frontier:
                return

    def _is_ringing(self, nodes) -> bool:
        """True if any ringing hint is on screen. For logging; never gates."""
        return any(any(h in name for h in RINGING_HINTS)
                   for _ctrl, name in nodes)

    def _pick(self, nodes, labels):
        """The best clickable control whose name is one of `labels`, or None.

        Ranked rather than first-match, and the ranking is the whole point.
        An exact label beats a substring match, because a bare substring rule is
        how a chat-list entry called "Accept my invite" gets clicked in the
        middle of answering. A substring match must also mention the call (see
        _CALL_CONTEXT) — that is what leaves the fallback able to catch an
        unlisted localisation while still refusing a conversation.
        """
        best: _Label | None = None
        best_rank = None
        for ctrl, name in nodes:
            exact = name in labels
            if not exact:
                if any(bad in name for bad in _NEVER_CLICK):
                    continue
                if not any(l in name for l in labels):
                    continue
                if not any(ctx in name for ctx in _CALL_CONTEXT):
                    continue
            rank = (0 if exact else 1, self._control_rank(ctrl))
            if best_rank is not None and rank >= best_rank:
                continue
            try:
                best, best_rank = _Label(ctrl, name, ctrl.rectangle()), rank
            except Exception:
                continue
        return best

    @staticmethod
    def _control_rank(ctrl) -> int:
        """Where this control sits in the Button → Text → anything order."""
        try:
            kind = ctrl.friendly_class_name()
        except Exception:
            return len(_CONTROL_TYPES)
        return _CONTROL_TYPES.index(kind) if kind in _CONTROL_TYPES \
            else len(_CONTROL_TYPES)

    def _click(self, label: _Label) -> bool:
        """Click a control, preferring its own input synthesis over a mouse.

        click_input() goes through pywinauto's own injection and finds the
        control even when something else holds focus. When it fails — the usual
        cause being a Chromium element reporting a stale rect — the rect is
        clicked with pyautogui, which is what the rest of this project's
        automation does.
        """
        if label is None:
            return False
        try:
            label.ctrl.click_input()
            return True
        except Exception:
            pass
        try:
            rect = label.rect
            # Read as plain attributes rather than rect.width(): every RECT
            # variant exposes these four, but only pywinauto's own subclass
            # carries the helpers, and there is no reason to depend on that here.
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return False
            import pyautogui                        # noqa: PLC0415
            pyautogui.click(*label.midpoint())
            return True
        except Exception as e:
            # FailSafeException lands here on purpose: a mouse parked in the
            # screen corner is the user saying stop, and retrying around it
            # would be the exact opposite of what they asked for.
            print(f"[CallAssistant] click on '{label.label}' failed: {e}")
            return False

    # ── the call itself ──────────────────────────────────────────────────────

    def _take_call(self, answer: _Label) -> bool:
        """Answer, speak, wait, say goodbye, hang up. True if it was answered."""
        with self._call_lock:
            if self._in_call:                       # one call at a time
                return False
            if not self._click(answer):
                self._log("ERR: Could not press the answer button — "
                          "this call has to be taken manually.")
                return False
            self._in_call = True

        started = time.monotonic()
        caller_heard = False
        greeted = False
        try:
            self._on_event("answered", {"label": answer.label})
            self._set_state(STATE_IN_CALL)
            self._stop.wait(CALL_SETTLE)            # let the call audio route

            if (time.monotonic() - started) >= self.max_call_seconds:
                # The ceiling is the one thing that may arrive before the
                # greeting — a call that connected into an already-exhausted
                # window would otherwise speak past it.
                print(f"[CallAssistant] call ceiling of "
                      f"{self.max_call_seconds}s reached before the greeting.")
            else:
                greeted = True
                self._speak(self._greeting())
                caller_heard = self._await_caller(
                    deadline=started + self.max_call_seconds)
                self._speak(self._farewell(caller_heard))
        except Exception as e:
            print(f"[CallAssistant] call failed: {type(e).__name__}: {e}")
        finally:
            self.end_call(reason=("greeting delivered" if greeted
                                  else "answered, nothing said"))
        return True

    def _await_caller(self, deadline: float) -> bool:
        """Wait out the caller. True if they were heard speaking.

        Three exits, in the order a person would pick them:
          * they spoke, then went quiet for `caller_grace` — their message is done
          * nothing for `silence_timeout` — nobody there, or they already left
          * `max_call_seconds` — the hard ceiling

        And one they chose: no call controls left on screen at all means they
        hung up, which is checked (rate-limited) on every pass.
        """
        monitor = self._open_monitor()
        quiet_since = time.monotonic()

        while not self._stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            if self._call_ended_by_caller(now):
                self._log("SYS: The other party hung up.")
                return True

            if monitor is not None and self._wait_for_caller:
                self._set_state(STATE_LISTENING)
                if monitor.heard_voice(now):
                    # Mid-sentence: reset the clock, hold until they stop, then
                    # give them one grace window and let them go.
                    quiet_since = time.monotonic()
                    while not self._stop.is_set():
                        if monitor.heard_voice():
                            quiet_since = time.monotonic()
                        elif (time.monotonic() - quiet_since) >= self.caller_grace:
                            break
                        self._stop.wait(0.3)
                    return True
            else:
                self._set_state(STATE_LISTENING)

            if (now - quiet_since) >= self.silence_timeout:
                break
            self._stop.wait(_VOICE_POLL)

        return bool(monitor is not None and monitor.heard_voice())

    def _call_ended_by_caller(self, now: float) -> bool:
        """True when WhatsApp shows no call controls at all — they hung up.

        Throttled to one scan per poll interval: it is a full accessibility walk,
        and on a call where nobody says anything this runs for the whole
        SILENCE_TIMEOUT.
        """
        if (now - self._last_hangup_check) < self.poll_interval:
            return False
        self._last_hangup_check = now

        win = self._whatsapp_window()
        if win is None:
            self._app = None
            return True
        try:
            nodes = list(self._scan(win))
        except Exception:
            return False
        if self._pick(nodes, DECLINE_LABELS) is not None:
            return False                     # still in a call, and ours to end
        # No end-call control. A live call always has one, so its absence is the
        # end of the call — not an empty tree to be retried.
        return True

    def end_call(self, reason: str = "") -> None:
        """Press Decline / End call and hand the avatar back. Never raises."""
        try:
            win = self._whatsapp_window()
            if win is not None:
                label = self._pick(list(self._scan(win)), DECLINE_LABELS)
                if label is not None:
                    self._click(label)
                    self._log(f"SYS: WhatsApp call ended ({reason}).")
                else:
                    self._log("SYS: WhatsApp call ended — no end-call button "
                              "to press, the other party had already hung up.")
            self._on_event("ended", {"reason": reason})
        except Exception as e:
            print(f"[CallAssistant] end_call: {type(e).__name__}: {e}")
        finally:
            self._monitor_stop()
            self._voice_stop()
            self._in_call = False
            self._set_state(STATE_IDLE)

    # ── what Zara says ───────────────────────────────────────────────────────

    def _greeting(self) -> str:
        return (
            f"Hello, I am {self.assistant_name}, {self.owner_name}'s AI "
            f"assistant. {self._owner_first} is currently busy. Please leave a "
            f"brief message or state your urgency."
        )

    @property
    def _owner_first(self) -> str:
        """The owner's first name.

        The announcement gives the full name once, so the caller knows who they
        have reached, and the short name from then on, because "Rustam is
        currently busy" is what a person says about someone they may well know
        while "Rustam Ali is currently busy" is what a switchboard says about a
        stranger. Derived from `owner_name` rather than configured separately —
        two spellings of one person is the drift core/brand.py exists to prevent.
        """
        first = (self.owner_name or "").strip().split(" ")[0]
        return first or self.owner_name

    def _farewell(self, caller_heard: bool) -> str:
        if caller_heard:
            return (f"Thank you. I have noted your message and "
                    f"{self.owner_name} will be told about it. Goodbye.")
        return (f"I did not hear anything, so I will let you go. "
                f"{self.owner_name} has been told you called. Goodbye.")

    def _speak(self, text: str) -> None:
        """Say one line to the caller, holding the avatar in Speaking."""
        voice = self._resolve_voice()
        self._set_state(STATE_SPEAKING)
        try:
            voice.speak(text)
        except Exception as e:
            print(f"[CallAssistant] speech failed: {type(e).__name__}: {e}")
        finally:
            # core.tts posts Idle through the voice-state hook itself; doing it
            # here as well would double-post on exactly one of these paths.
            if self._voice_kind != "app":
                self._set_state(STATE_IDLE)

    def _voice_stop(self) -> None:
        if self._voice is not None:
            try:
                self._voice.stop()
            except Exception:
                pass

    def _monitor_stop(self) -> None:
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()

    def _resolve_voice(self):
        """The speech engine, built once on first use.

        Order: whatever was injected, then pyttsx3, then Zara's configured
        offline voice, then silence. Each step down is a downgrade in quality
        but never in behaviour — the call is still answered and ended.
        """
        if self._voice is not None:
            return self._voice

        if self._speak_cb is not None:
            self._voice, self._voice_kind = _CallableVoice(self._speak_cb), "injected"
        else:
            try:
                self._voice, self._voice_kind = Pyttsx3Voice(), "pyttsx3"
            except Exception as e:
                print(f"[CallAssistant] pyttsx3 unavailable ({e}) — using "
                      f"Zara's configured offline voice instead.")
                try:
                    self._voice, self._voice_kind = _AppVoice(), "app"
                except Exception as e2:
                    print(f"[CallAssistant] no offline voice available ({e2}) "
                          f"— calls will be answered silently.")
                    self._voice, self._voice_kind = _NullVoice(), "none"
        return self._voice

    # ── caller audio plumbing ────────────────────────────────────────────────

    def _open_monitor(self) -> CallerAudioMonitor | None:
        if not self._wait_for_caller:
            return None
        if self._monitor is None:
            monitor = CallerAudioMonitor(device=self._resolve_call_device())
            if monitor.start():
                self._monitor = monitor
        return self._monitor

    def _resolve_call_device(self):
        """The sounddevice index for the call's microphone endpoint.

        The call's own endpoint, not Zara's. If a virtual cable is carrying this
        call then *its* input is the only place the caller's voice exists;
        listening on the laptop's built-in microphone hears the room instead.
        """
        if self._call_device_idx is not None:
            return self._call_device_idx
        name = self._call_device_name
        if not name:
            self._warn_if_no_bridge()
            return None
        try:
            from core import audio_devices              # noqa: PLC0415
            self._call_device_idx = audio_devices.resolve(name, "input")
        except Exception as e:
            print(f"[CallAssistant] could not resolve call device "
                  f"'{name}': {e}")
        return self._call_device_idx

    def _warn_if_no_bridge(self) -> None:
        """Say once, when the default output is a real device and not a bridge.

        Answering a call the other party cannot hear is the failure this whole
        feature exists to avoid, and it is invisible from inside the app: the
        write succeeded, the playback succeeded, and the sound went to a speaker
        in another room. Naming the endpoint turns a mystery into a fix.
        """
        if self._call_device_name or self._warned_route:
            return
        self._warned_route = True
        try:
            from pycaw.pycaw import AudioUtilities       # noqa: PLC0415
            endpoint = AudioUtilities.GetSpeakers().GetId()
        except Exception:
            return
        low = (endpoint or "").lower()
        if any(k in low for k in ("cable", "voicemeeter", "vb-audio", "virtual")):
            return
        self._log(
            f"ERR: WhatsApp calls cannot be heard through '{endpoint}'. "
            f"Bridge WhatsApp's microphone to it with VB-CABLE or VoiceMeeter, "
            f"or set a call_audio_device."
        )

    # ── avatar ───────────────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        try:
            self._on_state(state)
        except Exception as e:
            print(f"[CallAssistant] avatar state '{state}' failed: {e}")


# ── Wiring for main.py ────────────────────────────────────────────────────────

def start_call_listener(
    speak=None,
    on_state=None,
    log=None,
    on_event=None,
    **kwargs,
):
    """Build the assistant and start its daemon thread. Returns it, or None.

    main.py calls exactly this. `enabled` is read from the environment so the
    feature can be switched off without editing code, which is worth having for
    something that answers the owner's phone.
    """
    if os.name != "nt":
        print("[CallAssistant] WhatsApp call answering is Windows-only — off.")
        return None

    flag    = os.environ.get("ZARA_WHATSAPP_CALL_ASSISTANT", "1").strip().lower()
    enabled = flag in ("1", "on", "true", "yes")

    try:
        assistant = ZaraWhatsAppCallAssistant(
            speak=speak, on_state=on_state, log=log, on_event=on_event,
            enabled=enabled, **kwargs)
    except Exception as e:
        print(f"[CallAssistant] could not start: {e}")
        return None

    return assistant if assistant.start() else None


if __name__ == "__main__":       # pragma: no cover - manual check
    _demo = ZaraWhatsAppCallAssistant()
    print(f"Greeting: {_demo._greeting()}\n")
    _assistant = start_call_listener(log=lambda m: print(m))
    try:
        while _assistant is not None:
            time.sleep(1)
    except KeyboardInterrupt:
        if _assistant:
            _assistant.stop()
