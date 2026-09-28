"""The upload folder: a file dropped in is read, and a file still being copied
is not.

Run:  python _test_upload.py
"""
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS, FAIL = [], []


def check(name):
    def deco(fn):
        try:
            fn()
            PASS.append(name)
            print(f"  PASS  {name}")
        except Exception:
            FAIL.append((name, traceback.format_exc()))
            print(f"  FAIL  {name}")
        return fn
    return deco


from core.upload_watcher import (  # noqa: E402
    UploadWatcher, is_partial,
)

import main as zara_main  # noqa: E402


def make(folder, **kw):
    """A watcher that does not need a thread — poll_once() drives it."""
    got = []
    kw.setdefault("stable_secs", 0.01)
    kw.setdefault("poll_interval", 0.01)
    w = UploadWatcher(folder, on_file=got.append, **kw)
    w.got = got
    return w


def settle(w, ticks=6):
    """Run ticks until the watcher stops noticing new things."""
    out = []
    for _ in range(ticks):
        out += w.poll_once()
        time.sleep(0.01)
    return out


# ── partial-file detection ──────────────────────────────────────────────────
@check("mid-copy filenames are recognised as partial")
def _():
    for n in ("report.pdf.crdownload", "big.zip.part", "x.tmp",
              "~$budget.xlsx", "song.mp3.download", "._resource"):
        assert is_partial(n), n
    for n in ("report.pdf", "photo.png", "data.csv", "notes.txt",
              "archive.tar.gz", "a.tmpfile"):
        assert not is_partial(n), n


# ── the core behaviour ──────────────────────────────────────────────────────
@check("a dropped file is picked up")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td)
        (Path(td) / "hello.txt").write_text("hello", encoding="utf-8")
        got = settle(w)
        assert len(got) == 1, got
        assert got[0].name == "hello.txt", got
        assert w.got[0].name == "hello.txt", w.got


@check("a file still being written is NOT read")
def _():
    """The reason the watcher waits: an event-driven watcher fires on the
    first byte, and a half-copied JPEG decodes to a truncated-file error."""
    with tempfile.TemporaryDirectory() as td:
        w = make(td, stable_secs=0.3)
        p = Path(td) / "growing.bin"
        p.write_bytes(b"x" * 100)
        w.poll_once()                      # first sighting: records size
        w.poll_once()                      # still inside stable_secs

        # Grow it, the way a copy does.
        p.write_bytes(b"x" * 5000)
        w.poll_once()
        assert not w.got, f"read a file that was still growing: {w.got}"

        # Now let it settle.
        time.sleep(0.35)
        settle(w, ticks=3)
        assert len(w.got) == 1, f"never read the finished file: {w.got}"


@check("a partial-suffix file is ignored even once it is stable")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td)
        (Path(td) / "movie.mp4.crdownload").write_bytes(b"0" * 4096)
        settle(w, ticks=8)
        assert not w.got, f"read a .crdownload: {w.got}"


@check("files already in the folder are not re-read on startup")
def _():
    with tempfile.TemporaryDirectory() as td:
        for n in ("old1.txt", "old2.txt", "old3.txt"):
            (Path(td) / n).write_text("old", encoding="utf-8")
        w = make(td)
        assert w.seed_existing() == 3, "seed count wrong"
        settle(w, ticks=6)
        assert not w.got, f"re-read the backlog: {w.got}"

        # But a genuinely new one still arrives.
        (Path(td) / "new.txt").write_text("new", encoding="utf-8")
        settle(w, ticks=6)
        assert len(w.got) == 1 and w.got[0].name == "new.txt", w.got


@check("a zero-byte file is skipped and not retried forever")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td)
        (Path(td) / "empty.txt").write_bytes(b"")
        settle(w, ticks=6)
        assert not w.got, w.got
        assert (Path(td) / "empty.txt") in w.processed(), \
            "an empty file is still being retried"


@check("the same file is never processed twice")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td)
        p = Path(td) / "once.txt"
        p.write_text("data", encoding="utf-8")
        settle(w, ticks=6)
        n = len(w.got)
        settle(w, ticks=10)
        assert len(w.got) == n == 1, f"delivered {len(w.got)} times"


@check("forget() lets a file be re-read")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td)
        p = Path(td) / "again.txt"
        p.write_text("data", encoding="utf-8")
        settle(w, ticks=6)
        assert len(w.got) == 1, w.got
        w.forget(p)
        settle(w, ticks=6)
        assert len(w.got) == 2, f"forget() did not re-arm: {w.got}"


@check("a subfolder is not ingested")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td)
        d = Path(td) / "a whole project"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
        (d / "b.txt").write_text("x", encoding="utf-8")
        settle(w, ticks=8)
        assert not w.got, f"walked a dropped folder: {w.got}"


@check("a missing folder is created, not treated as an error")
def _():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "does" / "not" / "exist"
        w = make(target)
        assert w.ensure_folder() is True
        assert target.is_dir(), "folder was not created"


@check("pending() reports a file that is still settling")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = make(td, stable_secs=5.0)
        (Path(td) / "slow.bin").write_bytes(b"0" * 100)
        w.poll_once()
        assert [p.name for p in w.pending()] == ["slow.bin"], w.pending()


# ── the background thread ───────────────────────────────────────────────────
@check("the watcher thread notices a file on its own")
def _():
    with tempfile.TemporaryDirectory() as td:
        got = []
        w = UploadWatcher(td, on_file=got.append,
                          poll_interval=0.05, stable_secs=0.05)
        assert w.start() is True
        try:
            (Path(td) / "live.txt").write_text("hello", encoding="utf-8")
            deadline = time.time() + 5
            while not got and time.time() < deadline:
                time.sleep(0.05)
            assert got, "the thread never noticed the file"
            assert got[0].name == "live.txt", got
        finally:
            w.stop()
        assert not w.running, "stop() left the thread alive"


@check("a callback that raises does not kill the watcher")
def _():
    calls = []

    def _boom(p):
        calls.append(p)
        raise RuntimeError("cannot read this")

    with tempfile.TemporaryDirectory() as td:
        w = UploadWatcher(td, on_file=_boom,
                          poll_interval=0.05, stable_secs=0.05)
        logs = []
        w._log = logs.append
        w.start()
        try:
            (Path(td) / "a.txt").write_text("x", encoding="utf-8")
            (Path(td) / "b.txt").write_text("y", encoding="utf-8")
            deadline = time.time() + 6
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.05)
            assert len(calls) == 2, f"stopped after the first failure: {calls}"
            assert w.running, "the thread died"
            assert any("Could not read" in m for m in logs), logs
        finally:
            w.stop()


@check("stop() is clean and repeatable")
def _():
    with tempfile.TemporaryDirectory() as td:
        w = UploadWatcher(td, on_file=lambda p: None, poll_interval=0.05)
        w.start()
        w.stop()
        w.stop()
        assert not w.running


# ── end to end: the real handler ────────────────────────────────────────────
@check("an arrival is read and announced through the real handler")
def _():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "notes.txt"
        src.write_text("Shopping list:\n- milk\n- bread\n- coffee\n",
                       encoding="utf-8")

        z = zara_main.ZaraLive.__new__(zara_main.ZaraLive)
        z.ui = type("U", (), {
            "write_log": staticmethod(lambda m: logs.append(m)),
            "show_content": staticmethod(lambda lbl, txt: panel.append((lbl, txt))),
        })()
        z._asst_name = "Zara"
        z.session = None            # no live session: must not raise
        z._loop = None
        z._wake_enabled = True
        z._awake = True
        logs, panel = [], []
        z._on_upload_arrived(src)

        assert panel, "nothing was shown on the content panel"
        label, body = panel[0]
        assert "notes.txt" in label, label
        assert "milk" in body.lower() or len(body) > 0, body[:200]
        assert any("notes.txt" in m for m in logs), logs


@check("an unreadable arrival is logged, not raised")
def _():
    with tempfile.TemporaryDirectory() as td:
        junk = Path(td) / "broken.xyz"
        junk.write_bytes(b"\x00\x01\x02")

        z = zara_main.ZaraLive.__new__(zara_main.ZaraLive)
        logs = []
        z.ui = type("U", (), {
            "write_log": staticmethod(lambda m: logs.append(m)),
            "show_content": staticmethod(lambda *a: None),
        })()
        z._asst_name = "Zara"
        z.session = None
        z._loop = None
        z._wake_enabled = True
        z._awake = True
        try:
            z._on_upload_arrived(junk)
        except Exception as e:
            raise AssertionError(f"an unreadable file propagated: {e!r}")
        assert logs, "nothing was reported"


@check("the app's upload folder is the requested one")
def _():
    want = Path.home() / "Downloads" / "Zara's Upload"
    assert zara_main.UPLOAD_FOLDER == want, zara_main.UPLOAD_FOLDER
    assert "Zara" in str(zara_main.UPLOAD_FOLDER)


@check("_first_sentences keeps versions and file names intact")
def _():
    f = zara_main._first_sentences
    got = f("Version 1.2.3 shipped. report.final.txt is ready. And more text.")
    assert "1.2.3" in got, got
    assert "report.final.txt" in got, got
    assert f("no terminator here") == "no terminator here", "single line lost"
    assert f("") == ""
    assert len(f("word " * 200, 3, 60)) <= 61, "the cap was not applied"


@check("a CSV is read even without pandas installed")
def _():
    from core import gemini
    from actions.file_processor import file_processor

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sales.csv"
        p.write_text("region,total\nNorth,120\nSouth,340\nEast,95\n",
                     encoding="utf-8")

        seen = {}

        # _gemini_client() goes through gemini.call, not gemini.client — mocking
        # the wrong one let a real request out to the API.
        def _fake_call(contents, tier=None, config=None, timeout_ms=None, key=""):
            seen["prompt"] = contents if isinstance(contents, str) \
                else "\n".join(str(c) for c in contents)
            return type("R", (), {"text": "South leads with 340."})()

        real = gemini.call
        gemini.call = _fake_call
        try:
            out    = file_processor({"file_path": str(p), "action": "analyze"})
            info   = file_processor({"file_path": str(p), "action": "info"})
            stats  = file_processor({"file_path": str(p), "action": "stats"})
        finally:
            gemini.call = real

        assert "South leads" in out, f"no answer from the data: {out}"
        # The prompt must carry the real data, or the answer is invented.
        pr = seen.get("prompt", "")
        assert "North" in pr and "120" in pr, f"data missing from prompt:\n{pr}"
        assert "Rows: 3" in pr, pr

        assert "Rows: 3" in info and "Columns: 2" in info, info
        assert "min=95" in stats and "max=340" in stats, stats
        print(f"        info: {info.splitlines()[0]!r}")


@check("an empty CSV is reported, not crashed on")
def _():
    from actions.file_processor import file_processor

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.csv"
        p.write_text("", encoding="utf-8")
        out = file_processor({"file_path": str(p), "action": "analyze"})
        assert "empty" in out.lower(), out


@check("a CSV with a BOM does not corrupt the first column name")
def _():
    from core import gemini
    from actions.file_processor import _process_csv_without_pandas

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bom.csv"
        p.write_bytes("\ufeffname,value\nx,1\n".encode("utf-8"))
        seen = {}

        def _fake_call(contents, **k):
            seen["p"] = contents if isinstance(contents, str) \
                else "\n".join(str(c) for c in contents)
            return type("R", (), {"text": "ok"})()

        real = gemini.call
        gemini.call = _fake_call
        try:
            _process_csv_without_pandas(p, "analyze", {})
        finally:
            gemini.call = real
        pr = seen.get("p", "")
        assert "Columns: 2: name, value" in pr, \
            f"BOM leaked or count wrong: {pr[:200]}"
        assert "\ufeff" not in pr, "a BOM reached the prompt"


print()
print("=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 62)
for name, tb in FAIL:
    print(f"\n--- {name} ---\n{tb}")
sys.exit(1 if FAIL else 0)
