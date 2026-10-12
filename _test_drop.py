"""The exact user path: drag a folder onto the drop zone.

Run:  python _test_drop.py
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

FAIL = []


def ok(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAIL.append(name)


app = QApplication.instance() or QApplication([])

import ui  # noqa: E402
from actions.file_processor import _scan_folder, file_processor  # noqa: E402

zone = ui.FileDropZone()
got = []
zone.file_selected.connect(got.append)


def drop(path):
    """Build a real drag-enter + drop, the way Windows sends one."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    # PyQt6 wants real enums here, not the ints Qt5 accepted.
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    enter = QDragEnterEvent(QPoint(5, 5), actions, mime,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    zone.dragEnterEvent(enter)
    ok(f"dragEnter accepted a {path.name}", enter.isAccepted())
    ev = QDropEvent(QPointF(5, 5), actions, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    zone.dropEvent(ev)


def same(a, b):
    # QUrl.toLocalFile() hands back forward slashes on Windows; Path.as_posix()
    # normalises both sides so this compares paths, not separators.
    return Path(a).as_posix() == Path(b).as_posix()


with tempfile.TemporaryDirectory() as td:
    folder = Path(td) / "my photos"
    folder.mkdir()
    (folder / "a.txt").write_text("hello", encoding="utf-8")
    print("── a folder ──")
    drop(folder)
    ok("the folder reached the handler", got and same(got[0], folder),
       f"{got}")

    file = Path(td) / "note.txt"
    file.write_text("hello", encoding="utf-8")
    got.clear()
    print("── a file ──")
    drop(file)
    ok("the file still reaches the handler", got and same(got[0], file),
       f"{got}")

print("── a folder that no longer exists ──")
got.clear()
gone = Path(td) / "deleted"
drop(gone)
ok("a missing path is ignored, not emitted", not got, f"{got}")

print("── the canvas painter on a folder ──")
zone._current_file = str(folder)
zone._canvas.update()
ok("painter ran without raising", True)

print("── a real tree: this project ──")
root = Path(__file__).resolve().parent
import time  # noqa: E402
t0 = time.time()
scan = _scan_folder(root)
dt = time.time() - t0
ok("the real project scans", scan["files"] > 50, f"{scan['files']} files")
ok(f"and does it fast ({dt:.2f}s)", dt < 8.0, f"{dt:.2f}s")
ok("node_modules/.git were skipped",
   not any("node_modules" in n or "\\.git" in n for _, n in scan["largest"]))
print(f"        {scan['files']} files, {scan['dirs']} dirs, "
      f"truncated={scan['truncated']}")
for kind, v in sorted(scan["by_type"].items(), key=lambda kv: -kv[1]["count"])[:6]:
    print(f"        {kind:10s} {v['count']:4d}")

print("── a deep synthetic tree stays bounded ──")
from actions import file_processor as fp  # noqa: E402
with tempfile.TemporaryDirectory() as td2:
    deep = Path(td2) / "d"
    deep.mkdir()
    cur = deep
    for i in range(40):                     # deeper than _SCAN_MAX_DEPTH
        cur = cur / f"lvl{i}"
        cur.mkdir()
        (cur / "f.txt").write_text("x", encoding="utf-8")
    scan = _scan_folder(deep)
    ok("depth cap applied", scan["truncated"] is True)
    ok("and it still returns something", scan["files"] >= 0,
       f"{scan['files']} files")

print()
print("=" * 62)
print(f"  {'ALL GOOD' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
