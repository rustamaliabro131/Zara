"""The real thing: copy a real file into the real upload folder while a watcher
is running, exactly as Explorer would, and confirm Zara reads it.

Run:  python _test_upload_live.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAIL = []


def ok(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAIL.append(name)


import main as zara_main  # noqa: E402
from core.upload_watcher import UploadWatcher  # noqa: E402

FOLDER = zara_main.UPLOAD_FOLDER
ok("the upload folder is the requested path",
   FOLDER == Path.home() / "Downloads" / "Zara's Upload", str(FOLDER))

# A real file, not a synthetic blob: a PDF-shaped file, an image, a spreadsheet.
tmp = Path(tempfile.mkdtemp())
import PIL.Image  # noqa: E402

img = tmp / "photo.png"
PIL.Image.new("RGB", (48, 32), (200, 40, 40)).save(img)

csv = tmp / "sales.csv"
csv.write_text("region,total\nNorth,120\nSouth,340\nEast,95\n", encoding="utf-8")

got = []
logs = []
w = UploadWatcher(FOLDER, on_file=got.append,
                  poll_interval=0.2, stable_secs=0.2)
w._log = logs.append
ok("the watcher starts", w.start() is True)
w.seed_existing()

try:
    print("── copying a real image in, the way Explorer does ──")
    # shutil.copy2 writes in chunks, so this genuinely produces a growing file
    # — the case an event-driven watcher gets wrong.
    big = tmp / "big.png"
    PIL.Image.new("RGB", (900, 700), (10, 90, 200)).save(big)
    dest = FOLDER / big.name
    shutil.copy2(big, dest)

    deadline = time.time() + 20
    while not got and time.time() < deadline:
        time.sleep(0.1)
    ok("the image was noticed", bool(got), f"{[p.name for p in got]}")
    ok("it was the right file", got and got[0].name == big.name,
       got and str(got[0]))
    ok("it arrived complete",
       got and got[0].stat().st_size == big.stat().st_size,
       f"{got and got[0].stat().st_size} vs {big.stat().st_size}")

    print("── a spreadsheet ──")
    got.clear()
    dest2 = FOLDER / csv.name
    shutil.copy2(csv, dest2)
    deadline = time.time() + 20
    while not got and time.time() < deadline:
        time.sleep(0.1)
    ok("the csv was noticed", bool(got), f"{[p.name for p in got]}")
finally:
    w.stop()
    for n in ("big.png", csv.name):
        try:
            (FOLDER / n).unlink()
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)

print("── through Zara's own handler, with a stubbed model ──")
# The real path, with Gemini replaced so nothing is spent. file_processor's
# _gemini_client() goes through gemini.call, so that is what has to be replaced.
from core import gemini  # noqa: E402

fake_result = ("The document is a sales report. It lists three regions: North "
               "120, South 340, East 95.")
calls = []


def _fake_call(contents, tier=None, config=None, timeout_ms=None, key=""):
    calls.append(contents)
    return type("R", (), {"text": fake_result})()


panel, wlogs = [], []
real_call = gemini.call
gemini.call = _fake_call
try:
    z = zara_main.ZaraLive.__new__(zara_main.ZaraLive)
    z.ui = type("U", (), {
        "write_log": staticmethod(lambda m: wlogs.append(m)),
        "show_content": staticmethod(lambda lbl, txt: panel.append((lbl, txt))),
    })()
    z._asst_name = "Zara"
    z.session = None
    z._loop = None
    z._wake_enabled = True
    z._awake = True

    f = FOLDER / "report.csv"
    f.write_text("region,total\nNorth,120\n", encoding="utf-8")
    try:
        z._on_upload_arrived(f)
    finally:
        f.unlink(missing_ok=True)

    ok("the result reached the content panel", bool(panel), f"{panel}")
    if panel:
        label, body = panel[0]
        ok("the label names the file", "report.csv" in label, label)
        ok("the body has the extracted data",
           "North" in body and "120" in body, body[:160])
finally:
    gemini.call = real_call
    ok("the model was actually called (and it was the stub)",
       bool(calls), f"{len(calls)} call(s)")

print()
print("=" * 62)
print(f"  {'ALL GOOD' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
