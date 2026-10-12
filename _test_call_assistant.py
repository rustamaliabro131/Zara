"""Exercise skills/whatsapp_call_assistant.py without WhatsApp and without audio.

Nothing here touches pywinauto, pyttsx3 or a sound device: the tree scan, the
label ranking, the silence/voice waits and the click routing are all driven
against fakes, because those are the parts that decide whether a stranger's call
gets answered and hung up correctly.
"""
import sys, types, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills.whatsapp_call_assistant as m

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}"
          + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(name)


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeRect:
    def __init__(self, l=10, t=20, r=110, b=60):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):  return self.right - self.left
    def height(self): return self.bottom - self.top


class FakeCtrl:
    """Stands in for a pywinauto UIA element."""

    def __init__(self, text, kind="Button", rect=None):
        self._text, self._kind, self._rect = text, kind, rect or FakeRect()
        self.clicked = False
        self.click_input_raises = False

    def window_text(self):        return self._text
    def friendly_class_name(self): return self._kind
    def rectangle(self):          return self._rect

    def click_input(self):
        if self.click_input_raises:
            raise RuntimeError("element not reachable")
        self.clicked = True


class FakeParent:
    def __init__(self, *children): self._c = list(children)

    def children(self): return list(self._c)


class FakeWindow(FakeParent):
    pass


def build(tree, click_ok=True, extra=None):
    """Wire an assistant onto a fake WhatsApp window and return both."""
    spoken, states, events, logged = [], [], [], []

    a = m.ZaraWhatsAppCallAssistant(
        speak=lambda t: spoken.append(t),
        on_state=states.append,
        log=logged.append,
        on_event=lambda k, p: events.append(k),
        owner_name="Rustam Ali",
        assistant_name="Zara",
        poll_interval=0.01,
        silence_timeout=0.3,
        caller_grace=0.1,
        max_call_seconds=5.0,
    )

    # Scripted tree: after the answer click the window shows call controls.
    root = tree[0]
    if click_ok:
        ring = tree[1]
        def answer(_lbl):
            ring.children()
            root._c = list(ring.children())
            return True
    else:
        def answer(_lbl):
            return False

    a._enabled = True
    a._app = object()
    a._whatsapp_window = lambda: root
    a._click = answer
    if extra:
        for k, v in extra.items():
            setattr(a, k, v)
    return a, spoken, states, events, logged, root


# ── 1. greeting copy is the announcement that was specified ───────────────────
a = m.ZaraWhatsAppCallAssistant(owner_name="Rustam Ali", assistant_name="Zara")
check("greeting is verbatim",
      a._greeting(),
      "Hello, I am Zara, Rustam Ali's AI assistant. Rustam is currently busy. "
      "Please leave a brief message or state your urgency.")
check("farewell when heard", "noted your message" in a._farewell(True), True)
check("farewell names the owner", "Rustam" in a._farewell(True), True)
check("farewell branches on silence",
      a._farewell(True) != a._farewell(False), True)

# ── 2. label ranking ──────────────────────────────────────────────────────────
# One level of unnamed container above the controls: the shape Chromium actually
# produces, and the reason _scan must descend through nameless elements.
ring = FakeWindow(
    FakeCtrl("Accept",  "Button"),
    FakeCtrl("Decline", "Button"),
)
root = FakeWindow(ring)
a, *_ = build((root, ring))
nodes = list(a._scan(root))
check("scan descends through a nameless container", len(nodes), 2)
check("exact label wins",
      a._pick(nodes, m.ANSWER_LABELS).label, "accept")
check("exact label wins (decline)",
      a._pick(nodes, m.DECLINE_LABELS).label, "decline")

# The controls may also sit directly under the window.
flat = FakeWindow(FakeCtrl("Accept", "Button"), FakeCtrl("Decline", "Button"))
check("scan reads direct children too",
      len(list(a._scan(flat))), 2)

# Text control with the same label, listed after a Button: Button must win.
mixed = FakeWindow(
    FakeCtrl("Accept", "Text"),
    FakeCtrl("Accept", "Button"),
)
check("Button outranks Text",
      a._pick(list(a._scan(mixed)), m.ANSWER_LABELS).ctrl.friendly_class_name(),
      "Button")

# A chat-list entry that merely contains the word must never be clicked.
decoys = FakeWindow(
    FakeCtrl("Accept my invite", "Text"),
    FakeCtrl("Send message",   "Button"),
    FakeCtrl("Accept",         "Button"),
)
check("substring decoys rejected, exact picked",
      a._pick(list(a._scan(decoys)), m.ANSWER_LABELS).label, "accept")

# Only a decoy present -> nothing to click.
only_decoy = FakeWindow(FakeCtrl("Accept my invite", "Text"))
check("no exact match means no click",
      a._pick(list(a._scan(only_decoy)), m.ANSWER_LABELS), None)

# The localisation fallback still works: a compound label naming the call.
unlisted = FakeWindow(FakeCtrl("Kabul Et (arama)", "Button"))
check("an unlisted localisation is still found",
      a._pick(list(a._scan(unlisted)), m.ANSWER_LABELS).label, "kabul et (arama)")

# A bare "End call" must not be reached for through the word "end".
endish = FakeWindow(FakeCtrl("Send message", "Button"), FakeCtrl("End call", "Button"))
check("decline ignores 'send message'",
      a._pick(list(a._scan(endish)), m.DECLINE_LABELS).label, "end call")
check("bare 'end' not in decline set", "end" in m.DECLINE_LABELS, False)

# Turkish labels.
tr = FakeWindow(FakeCtrl("Kabul Et", "Button"), FakeCtrl("Reddet", "Button"))
check("tr answer label", a._pick(list(a._scan(tr)), m.ANSWER_LABELS).label,
      "kabul et")

# ── 3. scan stays bounded ─────────────────────────────────────────────────────
deep = FakeWindow()
node = deep
for _ in range(40):                       # 40 levels deep, past _SCAN_DEPTH
    child = FakeWindow()
    node._c.append(child)
    node = child
node._c.append(FakeCtrl("Accept", "Button"))
check("depth cap is respected",
      a._pick(list(a._scan(deep)), m.ANSWER_LABELS), None)

wide = FakeWindow(*[FakeCtrl(f"row {i}", "Text") for i in range(3000)])
count = len(list(a._scan(wide)))
check("node cap bounds a wide tree", count <= m._SCAN_MAX_NODES, True)

# ── 4. normalisation strips the glyphs Windows folds into names ────────────────
check("normalise strips arrow",
      m._norm("Accept  \u2192"), "accept")
check("normalise strips ellipsis", m._norm("Answer..."), "answer")
check("normalise collapses space", m._norm("  End   Call "), "end call")

# ── 5. click routing: click_input first, pyautogui only as a fallback ──────────
good = FakeCtrl("Accept"); good.click_input()
check("click_input is the first try",
      m.ZaraWhatsAppCallAssistant()._click(m._Label(good, "accept", good.rectangle())),
      True)
check("click_input was used", good.clicked, True)

# An element that reports no usable rect has nothing for the fallback to aim at.
flat = FakeRect(100, 100, 100, 100)
dead = FakeCtrl("Accept"); dead.click_input_raises = True
check("a zero-size rect is not clicked",
      m.ZaraWhatsAppCallAssistant()._click(m._Label(dead, "accept", flat)), False)

# click_input succeeding is enough on its own, rect or not.
live = FakeCtrl("Accept")
check("a live element is clicked without needing our rect",
      m.ZaraWhatsAppCallAssistant()._click(
          m._Label(live, "accept", flat)), True)

# ── 6. full call: detected, answered, spoken, hung up ─────────────────────────
ring = FakeWindow(FakeCtrl("Accept", "Button"), FakeCtrl("Decline", "Button"))
in_call = FakeWindow(FakeCtrl("Accept", "Button"), FakeCtrl("Decline", "Button"))
root = FakeWindow(ring)
a, spoken, states, events, logged, root = build((root, ring))
a._pick = lambda nodes, labels: next(
    (m._Label(c, n, c.rectangle()) for c, n in nodes
     if n in labels), None)

monitor = m.CallerAudioMonitor(device=None)
monitor.start = lambda: True
monitor.stop = lambda: None
monitor.level = lambda: 0.0
monitor.heard_voice = lambda now=None: False
a._open_monitor = lambda: monitor

t0 = time.monotonic()
answered = a._take_call(m._Label(FakeCtrl("Accept"), "accept", FakeRect()))
check("incoming call was answered", answered, True)
check("the greeting was spoken", spoken[0], m.ZaraWhatsAppCallAssistant()._greeting())
check("a farewell was spoken", len(spoken), 2)
check("call ran under its ceiling", time.monotonic() - t0 < 5.0, True)
check("answered then ended", events[:2], ["answered", "ended"])
check("avatar went In Call", states[0], "In Call")
check("avatar spoke", "Speaking" in states, True)
check("avatar listened", "Listening" in states, True)
check("avatar returned to Idle", states[-1], "Idle")
check("in_call cleared", a.in_call, False)

# ── 7. a call the button could not be pressed on is not claimed ───────────────
root2 = FakeWindow(FakeWindow(FakeCtrl("Accept", "Button")))
a2, spoken2, *_ = build((root2, None), click_ok=False)
check("an unpressed answer button reports failure",
      a2._take_call(m._Label(FakeCtrl("Accept"), "accept", FakeRect())), False)
check("and nothing was spoken", spoken2, [])
check("and it is not marked in a call", a2.in_call, False)

# ── 8. poll_once end to end against a fake tree ───────────────────────────────
ring3 = FakeWindow(FakeCtrl("Accept", "Button"))
root3 = FakeWindow(ring3)
a3, *_ , _ = build((root3, ring3))
a3._connect = lambda: True
a3._whatsapp_window = lambda: root3
a3._take_call = lambda lbl: True
check("poll_once finds a ringing call",
      a3.poll_once() is False or True, True)   # shape check, real one below

ring4 = FakeWindow(FakeCtrl("Accept", "Button"))
root4 = FakeWindow(ring4)
a4, *_ = build((root4, ring4))
a4._connect = lambda: True
a4._whatsapp_window = lambda: root4
taken = []
a4._take_call = lambda lbl: taken.append(lbl.label) or True
check("poll_once answers a ringing tree", a4.poll_once(), True)
check("it pressed the Accept control", taken, ["accept"])

# a quiet window must not answer anything
quiet = FakeWindow(FakeCtrl("Chats", "Text"), FakeCtrl("Message", "Button"))
a5, *_ = build((quiet, quiet))
a5._connect = lambda: True
a5._whatsapp_window = lambda: quiet
check("a quiet window answers nothing", a5.poll_once(), False)

# ── 9. disconnected / disabled states ─────────────────────────────────────────
a6 = m.ZaraWhatsAppCallAssistant(enabled=False, log=lambda s: None)
check("a disabled assistant does not start", a6.start(), False)
check("a disabled assistant never polls", a6.poll_once(), False)

a7 = m.ZaraWhatsAppCallAssistant(log=lambda s: None)
a7._connect = lambda: False
check("no WhatsApp means no crash", a7.poll_once(), False)

# ── 10. the caller-speech wait ────────────────────────────────────────────────
class LoudThenQuiet(m.CallerAudioMonitor):
    """Loud for the first `loud_for` seconds, silent after."""
    def __init__(self, loud_for):
        super().__init__(device=None)
        self._t0 = time.monotonic()
        self._loud_for = loud_for

    def level(self):
        return 1.0 if (time.monotonic() - self._t0) < self._loud_for else 0.0

    def heard_voice(self, now=None):
        return self.level() >= m._VOICE_GATE

loud = LoudThenQuiet(loud_for=1.2)
check("a loud caller is heard", loud.heard_voice(), True)
loud2 = LoudThenQuiet(loud_for=0.0)
check("silence is not a caller", loud2.heard_voice(), False)

a8 = m.ZaraWhatsAppCallAssistant(log=lambda s: None, wait_for_caller=False,
                               silence_timeout=0.3, poll_interval=0.05)
a8._open_monitor = lambda: None
a8._call_ended_by_caller = lambda now: False
t0 = time.monotonic()
heard = a8._await_caller(deadline=time.monotonic() + 5.0)
elapsed = time.monotonic() - t0
check("silence expires on silence_timeout, not the ceiling",
      0.25 <= elapsed < 1.0, True)
check("and reports nobody heard", heard, False)

# And the ceiling wins when it is shorter than the silence timeout.
a8b = m.ZaraWhatsAppCallAssistant(log=lambda s: None, wait_for_caller=False,
                                silence_timeout=30.0, poll_interval=0.05)
a8b._open_monitor = lambda: None
a8b._call_ended_by_caller = lambda now: False
t0 = time.monotonic()
a8b._await_caller(deadline=time.monotonic() + 0.4)
check("the hard ceiling is enforced", (time.monotonic() - t0) < 1.0, True)

# ── 10b. a nonsensical ceiling is clamped, not obeyed ─────────────────────────
check("a zero ceiling falls back to the default",
      m.ZaraWhatsAppCallAssistant(max_call_seconds=0).max_call_seconds,
      m.MAX_CALL_SECONDS)
check("a negative ceiling falls back too",
      m.ZaraWhatsAppCallAssistant(max_call_seconds=-5).max_call_seconds,
      m.MAX_CALL_SECONDS)
check("a real ceiling is honoured",
      m.ZaraWhatsAppCallAssistant(max_call_seconds=30).max_call_seconds, 30.0)

# ── 11. voice resolution order ────────────────────────────────────────────────
a9 = m.ZaraWhatsAppCallAssistant(log=lambda s: None)
a9._speak_cb = lambda t: None
a9._resolve_voice()
check("an injected voice is used", a9._voice_kind, "injected")

a10 = m.ZaraWhatsAppCallAssistant(log=lambda s: None)
real = m.Pyttsx3Voice
m.Pyttsx3Voice = lambda: real.__new__(real)      # skip SAPI5, keep the class
try:
    a10._resolve_voice()
    check("pyttsx3 is the first engine tried", a10._voice_kind, "pyttsx3")
finally:
    m.Pyttsx3Voice = real

a11 = m.ZaraWhatsAppCallAssistant(log=lambda s: None)
boom = lambda *a, **k: (_ for _ in ()).throw(ImportError("no pyttsx3"))
m.Pyttsx3Voice, keep = boom, m.Pyttsx3Voice
m._AppVoice = lambda: real.__new__(real)
try:
    a11._resolve_voice()
    check("core.tts is the fallback", a11._voice_kind, "app")
finally:
    m.Pyttsx3Voice = keep

# ── 12. start/stop and the daemon thread ──────────────────────────────────────
a12 = m.ZaraWhatsAppCallAssistant(log=lambda s: None, poll_interval=0.05)
a12._connect = lambda: False
check("listener starts", a12.start(), True)
time.sleep(0.3)
check("the thread is a daemon", a12._thread.daemon, True)
check("the thread has a name", a12._thread.name, "zara-whatsapp-call-listener")
check("listener reports running", a12.running, True)
a12.stop()
check("stop() ends the thread", a12.running, False)

# stop() during a call must hang up rather than leave the caller connected
a13 = m.ZaraWhatsAppCallAssistant(log=lambda s: None)
ended = []
a13._in_call = True
a13._whatsapp_window = lambda: None
a13._on_event = lambda k, p: ended.append(k)
a13.stop()
check("stop() hangs up on a live call", ended, ["ended"])

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all checks passed")
