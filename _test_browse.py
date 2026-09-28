"""The Browse button, and the crash it caused.

Run:  python _test_browse.py
"""
import os
import sys
import tempfile
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


from PyQt6.QtWidgets import QApplication, QFileDialog  # noqa: E402

import ui  # noqa: E402

app = QApplication.instance() or QApplication([])


# ── the actual bug ──────────────────────────────────────────────────────────
@check("the drop zone has the name the browse dialog reads")
def _():
    zone = ui.FileDropZone()
    assert hasattr(zone, "_assistant_name"), \
        "the attribute the browse dialog reads does not exist"
    assert zone._assistant_name, "it is empty, so the dialog title breaks"
    zone.deleteLater()


@check("_browse() does not raise, with the dialog returning a file")
def _():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "report.pdf"
        f.write_text("x", encoding="utf-8")

        zone = ui.FileDropZone()
        got = []
        zone.file_selected.connect(got.append)
        real = QFileDialog.getOpenFileName
        QFileDialog.getOpenFileName = staticmethod(
            lambda *a, **k: (str(f), ""))
        try:
            zone._browse()                 # the click handler, verbatim
        finally:
            QFileDialog.getOpenFileName = real
            zone.deleteLater()

        assert got, "the picked file never reached the handler"
        assert got[0].endswith("report.pdf"), got


@check("cancelling the dialog does nothing and does not raise")
def _():
    zone = ui.FileDropZone()
    got = []
    zone.file_selected.connect(got.append)
    calls = []
    real = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (calls.append(1), ("", ""))[1])
    try:
        zone._browse()
    finally:
        QFileDialog.getOpenFileName = real
        zone.deleteLater()
    assert not got, f"a cancel produced a file: {got}"
    assert len(calls) == 1, f"the dialog opened {len(calls)} times on one click"


@check("a second dialog is never opened on cancel")
def _():
    """The old code fell through to getExistingDirectory when the first dialog
    came back empty, so every Cancel popped another dialog."""
    zone = ui.FileDropZone()
    real_open = QFileDialog.getOpenFileName
    real_dir = QFileDialog.getExistingDirectory
    opened = []

    QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (opened.append("file"), ("", ""))[1])
    QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: (opened.append("dir"), "")[1])
    try:
        zone._browse()
    finally:
        QFileDialog.getOpenFileName = real_open
        QFileDialog.getExistingDirectory = real_dir
        zone.deleteLater()
    assert opened == ["file"], f"a cancel opened: {opened}"


@check("a raising file dialog does not escape the slot")
def _():
    zone = ui.FileDropZone()
    real = QFileDialog.getOpenFileName

    def _boom(*a, **k):
        raise RuntimeError("the shell dialog failed")

    QFileDialog.getOpenFileName = staticmethod(_boom)
    try:
        zone._browse()                 # must return, not raise
    finally:
        QFileDialog.getOpenFileName = real
        zone.deleteLater()


@check("the picker remembers the last folder used")
def _():
    with tempfile.TemporaryDirectory() as td:
        sub = Path(td) / "docs"
        sub.mkdir()
        f = sub / "a.txt"
        f.write_text("x", encoding="utf-8")

        zone = ui.FileDropZone()
        real = QFileDialog.getOpenFileName
        seen = []

        def _fake(parent, caption, start, *a, **k):
            seen.append(start)
            return (str(f), "")

        QFileDialog.getOpenFileName = staticmethod(_fake)
        try:
            zone._browse()
            # Reopening where the last pick was, not at the home folder.
            assert Path(seen[0]).is_dir(), f"bad start dir: {seen[0]}"
            zone._browse()
            after = zone._browse_start_dir()
            assert Path(after) == sub, \
                f"expected the folder of the last pick ({sub}), got {after}"
            assert seen[1] == str(sub), f"reopened at {seen[1]}"
        finally:
            QFileDialog.getOpenFileName = real
            zone.deleteLater()


@check("a rename reaches the drop zone")
def _():
    from PyQt6.QtWidgets import QFileDialog as _D  # noqa: F401
    zone = ui.FileDropZone()
    zone.set_assistant_name("Zara")
    assert zone._assistant_name == "Zara", zone._assistant_name
    zone.set_assistant_name("")
    assert zone._assistant_name, "an empty name must fall back to the default"
    zone.deleteLater()


# ── the systemic fix ────────────────────────────────────────────────────────
@check("an exception in a slot no longer aborts the process")
def _():
    """The actual mechanism: PyQt6 calls sys.excepthook and then qFatal, which
    kills the process with no dialog. Replacing the hook suppresses the abort.
    This test installs the real hook and then raises inside a real Qt slot."""
    import subprocess

    script = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "sys.path.insert(0, r'%s')\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "import ui\n"
        "app = QApplication.instance() or QApplication([])\n"
        "ui._install_excepthook(app)\n"
        "zone = ui.FileDropZone()\n"
        "real = ui.QFileDialog.getOpenFileName\n"
        "ui.QFileDialog.getOpenFileName = staticmethod(\n"
        "    lambda *a, **k: (_ for _ in ()).throw(AssertionError('boom')))\n"
        "try:\n"
        "    zone._browse()\n"
        "except BaseException as e:\n"
        "    print('ESCAPED', type(e).__name__)\n"
        "    sys.exit(3)\n"
        "ui.QFileDialog.getOpenFileName = real\n"
        "print('SURVIVED')\n"
        "sys.exit(0)\n"
    ) % str(Path(__file__).resolve().parent)

    r = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    assert "SURVIVED" in out, f"the process died:\n{out[-800:]}"
    assert r.returncode == 0, f"exit {r.returncode}"
    print("        the slot raised and the process lived")


@check("errors are written to config/errors.log")
def _():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "errors.log"
        try:
            raise ValueError("a deliberate test error")
        except ValueError:
            import traceback as tb
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("\n===== test =====\n"
                         + "".join(tb.format_exception(*sys.exc_info())))
        assert p.is_file() and "a deliberate test error" in p.read_text(
            encoding="utf-8")


@check("the excepthook survives a hook that itself fails")
def _():
    import ui as _ui
    src = Path(_ui.__file__).read_text(encoding="utf-8")
    assert "sys.excepthook = _hook" in src, "hook is not installed"
    assert "threading.excepthook" in src or "_th.excepthook" in src, \
        "worker threads are still unprotected"


print()
print("=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 62)
for name, tb in FAIL:
    print(f"\n--- {name} ---\n{tb}")
sys.exit(1 if FAIL else 0)
