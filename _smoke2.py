"""End-to-end sanity: the real ZaraUI starts, the icon is on the window, and a
vision request reaches a REST model instead of the Live session.

Run:  python _smoke2.py
"""
import asyncio
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONUTF8", "1")

FAIL = []


def ok(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAIL.append(name)


# ── 1. The real UI starts and shows the icon ────────────────────────────────
print("── UI ──")
from PyQt6.QtWidgets import QApplication  # noqa: E402

import ui  # noqa: E402

app = QApplication.instance() or QApplication([])
win = ui.MainWindow(ui.CONFIG_DIR / "nonexistent-face.png")
try:
    ok("window icon set", not win.windowIcon().isNull())
    ok("window icon has sizes", bool(win.windowIcon().availableSizes()))
    ok("title keeps the version", ui.APP_VERSION in win.windowTitle(),
       repr(win.windowTitle()))
    ok("icon is not the null fallback", "zara" in str(ui.ICON_PNG).lower())
finally:
    win.close()
    win.deleteLater()


# ── 2. Vision goes to REST, and the Live session never sees the frame ───────
print("── vision routing ──")
from core import gemini  # noqa: E402

seen = []


class _Models:
    def generate_content(self, model=None, contents=None, config=None):
        kinds = []
        for c in (contents if isinstance(contents, list) else [contents]):
            if isinstance(c, str):
                kinds.append("text")
            elif isinstance(c, dict) and (c.get("inline_data") or c.get("inlineData")):
                kinds.append("image")
            elif getattr(c, "inline_data", None) is not None:
                # An SDK Part carrying bytes — the shape a real frame takes.
                kinds.append("image")
            else:
                kinds.append("part")
        seen.append((model, tuple(kinds)))
        return type("R", (), {"text": "The screen shows a code editor."})()


real_client, real_live = gemini.client, gemini._live_call
gemini.client = lambda timeout_ms=0, key="": type("C", (), {"models": _Models()})()


def _boom(*a, **k):
    raise AssertionError("Live session was opened for a vision frame")


gemini._live_call = _boom
try:
    from google.genai import types as gtypes

    jpeg = Path(ui.CONFIG_DIR / "zara.jpg")
    if not jpeg.is_file():
        import io

        import PIL.Image
        buf = io.BytesIO()
        PIL.Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
        payload = buf.getvalue()
    else:
        payload = jpeg.read_bytes()

    part = gtypes.Part.from_bytes(data=payload, mime_type="image/jpeg")
    out = gemini.text(["what is on my screen?", part], tier=gemini.SMART)
    ok("vision answered", bool(out), out[:60])
    ok("vision used REST, not Live", all(m != gemini.LIVE for m, _ in seen),
       f"{[m for m, _ in seen]}")
    ok("the frame was actually sent", any("image" in k for _, k in seen),
       f"{seen}")

    # And the Live rung still works for text.
    seen.clear()

    def _live_text(contents, config, timeout_ms, key):
        seen.append((gemini.LIVE, ("text",)))
        return type("R", (), {"text": "spoken reply"})()

    gemini._live_call = _live_text
    out = gemini.text("hello there", tier=gemini.SMART)
    ok("text still uses Live", seen and seen[0][0] == gemini.LIVE, f"{seen}")
    ok("text answer returned", out == "spoken reply", out)
finally:
    gemini.client, gemini._live_call = real_client, real_live


# ── 3. The pending-vision path builds a real answer and speaks it ───────────
print("── main.py vision path ──")
import main as zara_main  # noqa: E402

ok("_is_modality_error is callable", callable(zara_main._is_modality_error))
ok("_is_modality_error catches the real error",
   zara_main._is_modality_error(
       'Cannot read "image.png" (this model does not support image input).'))
ok("_is_modality_error ignores tuning drift",
   not zara_main._is_modality_error(
       'unknown name "media_resolution" at `session.send_client_content`'))
ok("ZaraLive._describe_frame exists",
   hasattr(zara_main.ZaraLive, "_describe_frame"))


class _FakeSession:
    def __init__(self):
        self.sent = []

    async def send_client_content(self, turns=None, turn_complete=False):
        self.sent.append((turns, turn_complete))


async def _drive():
    z = zara_main.ZaraLive.__new__(zara_main.ZaraLive)
    z._pending_vision = (b"\xff\xd8\xff\xd9", "image/jpeg",
                         "what is on my screen?", "screen")
    z.session = _FakeSession()
    # The bookkeeping the finally block touches, in the state __init__ leaves.
    z._vision_cam_active = False
    z._vision_close_pending = False
    z._vision_busy = True

    def _describe(img_b, mime_t, question):
        assert img_b == b"\xff\xd8\xff\xd9", img_b
        assert mime_t == "image/jpeg", mime_t
        assert "SCREEN CAPTURE" in question, question
        return "A code editor with a red banner."

    z._describe_frame = _describe
    flag = await z._flush_pending_vision()
    return flag, z.session.sent, z._pending_vision, z._vision_busy


flag, sent, cleared, busy = asyncio.run(_drive())
ok("_flush_pending_vision reports success", flag)
ok("exactly one turn was sent", len(sent) == 1, f"{len(sent)} turns")
ok("the turn completes", sent and sent[0][1] is True)
text = sent[0][0]["parts"][0]["text"] if sent else ""
ok("no image bytes in the turn", "inline_data" not in sent[0][0]["parts"][0])
ok("the description is in the turn", "code editor" in text.lower(), text[:80])
ok("the answer is not read verbatim", "do not read" in text.lower())
ok("the frame was consumed", cleared is None, repr(cleared))
ok("_vision_busy released on success", busy is False, repr(busy))


# A vision failure must not kill the session, and must not leave the app stuck.
async def _drive_fail():
    z = zara_main.ZaraLive.__new__(zara_main.ZaraLive)
    z._pending_vision = (b"\xff\xd8", "image/jpeg", "what?", "screen")
    z.session = _FakeSession()
    z.ui = type("U", (), {"write_log": staticmethod(lambda m: None),
                          "set_state": staticmethod(lambda s: None)})()
    z._vision_cam_active = False
    z._vision_close_pending = False
    z._vision_busy = True

    def _bad(*a, **k):
        raise RuntimeError("vision model unreachable")

    z._describe_frame = _bad
    z.set_speaking = lambda v: None
    ret = await z._flush_pending_vision()
    return ret, z.session.sent, z._vision_busy


returned, sent2, busy2 = asyncio.run(_drive_fail())
ok("a vision failure returns cleanly", returned is False, repr(returned))
ok("a vision failure sends no turn", not sent2, f"{sent2}")
ok("_vision_busy released on failure", busy2 is False,
   "a failed vision call would block every later one")

print()
print("=" * 62)
print(f"  {'ALL GOOD' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
