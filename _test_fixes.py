"""Checks for the icon, image-routing and folder-upload fixes.

Run:  python _test_fixes.py
Exits non-zero on the first failure so it can gate a commit.
No network: every Gemini call is stubbed.
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS, FAIL = [], []

# QApplication must exist before ANY QObject/QPixmap/QIcon is constructed —
# building a QIcon first is a hard native crash on Windows, not a Python
# exception, so it cannot be caught and just kills the run.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])


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


# ── 1. The image that caused the crash must never reach a Live session ────────
@check("gemini._has_image detects SDK image parts")
def _():
    from google.genai import types as gtypes
    from core import gemini

    part = gtypes.Part.from_bytes(data=b"\xff\xd8\xff", mime_type="image/jpeg")
    assert gemini._has_image([part]), "image Part not detected"
    assert not gemini._has_image("describe this"), "plain text flagged as image"
    assert not gemini._has_image(["a", "b"]), "list of text flagged as image"
    assert not gemini._has_image(
        [gtypes.Part.from_bytes(data=b"hi", mime_type="text/plain")]
    ), "text/plain part flagged as image"


@check("gemini._has_image detects raw Pillow images")
def _():
    import io

    import PIL.Image
    from core import gemini

    buf = io.BytesIO()
    PIL.Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    buf.seek(0)
    img = PIL.Image.open(buf)
    assert gemini._has_image([img]), "PIL image not detected"
    assert not gemini._has_image([1, 2, 3]), "plain ints flagged as image"


@check("gemini._to_live_parts drops images but keeps text")
def _():
    from google.genai import types as gtypes
    from core import gemini

    part = gtypes.Part.from_bytes(data=b"\xff\xd8\xff", mime_type="image/jpeg")
    parts = gemini._to_live_parts([part, "describe this"])
    kinds = [k for p in parts for k in p]
    assert "inline_data" not in kinds, f"image forwarded to Live: {parts}"
    assert "text" in kinds, f"text lost: {parts}"
    assert parts[0] == {"text": "describe this"}, parts


@check("gemini._live_call refuses an image outright")
def _():
    from google.genai import types as gtypes
    from core import gemini

    part = gtypes.Part.from_bytes(data=b"\xff\xd8\xff", mime_type="image/jpeg")
    try:
        gemini._live_call([part, "describe"], None, 5_000, "k")
    except ValueError as e:
        assert "does not support" in str(e), e
    else:
        raise AssertionError("_live_call accepted an image — the crash is back")


@check("an image payload removes the Live rung from every ladder")
def _():
    from google.genai import types as gtypes
    from core import gemini

    part = gtypes.Part.from_bytes(data=b"\xff\xd8\xff", mime_type="image/jpeg")
    for tier in (gemini.FAST, gemini.SMART):
        ladder = gemini._LADDERS[tier]
        assert gemini.LIVE in ladder, f"{tier} has no Live rung to test with"
        rest_only = tuple(m for m in ladder if m != gemini.LIVE)
        assert rest_only, f"{tier} would have nothing left for images"


class _FakeModels:
    """`cl.models` — an object with generate_content, which is what the real
    SDK exposes. A plain function on the class is not the same shape and hides
    real breakage."""

    def __init__(self, used, reply="a red square"):
        self.used, self.reply = used, reply

    def generate_content(self, model=None, contents=None, config=None):
        self.used.append(model)
        return type("R", (), {"text": self.reply})()


class _FakeClient:
    def __init__(self, used, reply="a red square"):
        self.models = _FakeModels(used, reply)


@check("call() routes an image to REST and never calls the Live model")
def _():
    from google.genai import types as gtypes
    from core import gemini

    part = gtypes.Part.from_bytes(data=b"\xff\xd8\xff", mime_type="image/jpeg")
    used = []

    def _no_live(*a, **k):
        raise AssertionError("Live was called for an image payload")

    real_client, real_live = gemini.client, gemini._live_call
    gemini.client = lambda timeout_ms=0, key="": _FakeClient(used)
    gemini._live_call = _no_live
    try:
        out = gemini.text([part, "what is this?"], tier=gemini.SMART)
    finally:
        gemini.client, gemini._live_call = real_client, real_live

    assert out == "a red square", out
    assert used, "no REST model was called"
    assert all(m != gemini.LIVE for m in used), f"Live was used: {used}"
    print(f"        image went to: {used[0]}")


@check("a PIL image payload also skips Live")
def _():
    import io

    import PIL.Image
    from core import gemini

    buf = io.BytesIO()
    PIL.Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
    buf.seek(0)
    img = PIL.Image.open(buf)

    used = []

    def _no_live(*a, **k):
        raise AssertionError("Live was called for a Pillow image payload")

    real_client, real_live = gemini.client, gemini._live_call
    gemini.client = lambda timeout_ms=0, key="": _FakeClient(used, "a blue square")
    gemini._live_call = _no_live
    try:
        out = gemini.text([img, "what is this?"], tier=gemini.SMART)
    finally:
        gemini.client, gemini._live_call = real_client, real_live
    assert used and all(m != gemini.LIVE for m in used), used
    print(f"        PIL image went to: {used[0]}")


@check("text-only calls still lead with the Live rung")
def _():
    from core import gemini

    used = []

    def _live_ok(contents, config, timeout_ms, key):
        used.append(gemini.LIVE)
        return type("R", (), {"text": "ok"})()

    real_client, real_live = gemini.client, gemini._live_call
    gemini.client = lambda timeout_ms=0, key="": _FakeClient(used)
    gemini._live_call = _live_ok
    try:
        out = gemini.text("hello", tier=gemini.SMART)
    finally:
        gemini.client, gemini._live_call = real_client, real_live
    assert out == "ok", out
    assert used == [gemini.LIVE], (
        f"text should still prefer the Live rung, got {used}")


# ── 2. Folders are a real upload, not a silent no-op ──────────────────────────
def _tree(root: Path):
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("X = 1\n", encoding="utf-8")
    (root / "docs" / "readme.md").write_text("# Title\nSome prose.\n", encoding="utf-8")
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1")
    (root / ".hidden").write_text("secret", encoding="utf-8")


@check("file_processor accepts a folder instead of refusing it")
def _():
    from actions.file_processor import file_processor

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "project"
        root.mkdir()
        _tree(root)
        out = file_processor({"file_path": str(root), "action": "list"})
        assert "Path is not a file" not in out, out
        assert str(root) in out, out
        assert ".py" in out, f"types missing from the inventory:\n{out}"


@check("the folder scan skips node_modules and dotfiles")
def _():
    from actions.file_processor import _scan_folder

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "p"
        root.mkdir()
        _tree(root)
        scan = _scan_folder(root)
        assert scan["files"] == 4, f"expected 4 real files, got {scan['files']}"
        names = " ".join(n for _, n in scan["largest"])
        assert "node_modules" not in names, f"walked node_modules: {names}"
        assert ".hidden" not in names, f"counted a dotfile: {names}"


@check("the folder scan is bounded")
def _():
    from actions import file_processor as fp

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "big"
        (root / "d").mkdir(parents=True)
        real = fp._SCAN_MAX_FILES
        fp._SCAN_MAX_FILES = 10          # force the cap without 4000 files
        try:
            for i in range(60):
                (root / "d" / f"f{i}.txt").write_text("x", encoding="utf-8")
            scan = fp._scan_folder(root)
        finally:
            fp._SCAN_MAX_FILES = real
        assert scan["files"] == 10, scan["files"]
        assert scan["truncated"] is True, "truncation not flagged in the manifest"


@check("an empty folder says so instead of crashing")
def _():
    from actions.file_processor import file_processor

    with tempfile.TemporaryDirectory() as td:
        out = file_processor({"file_path": td, "action": "list"})
        assert "no readable files" in out.lower(), out


@check("a missing path is reported, not raised")
def _():
    from actions.file_processor import file_processor

    out = file_processor({"file_path": r"C:\nope\missing.png"})
    assert "not found" in out.lower(), out
    out = file_processor({})
    assert "no file path" in out.lower(), out


@check("a real file still routes to its own handler")
def _():
    import io

    import PIL.Image
    from actions.file_processor import file_processor

    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "pic.png"
        PIL.Image.new("RGB", (16, 16), (200, 10, 10)).save(fp)
        out = file_processor({"file_path": str(fp), "action": "info"})
        assert "Path is not a file" not in out, out
        assert "Path is neither" not in out, out
        assert out.strip(), "file handler returned nothing"


# ── 3. The window icon is actually applied ───────────────────────────────────
@check("the Zara icon files exist and are valid")
def _():
    from PyQt6.QtGui import QIcon

    for name, sizes in (("config/zara.png", None), ("config/zara.ico", None)):
        p = Path(name)
        assert p.is_file(), f"missing {name}"
        ic = QIcon(str(p))
        assert not ic.isNull(), f"{name} is not a loadable icon"

    import PIL.Image
    ico = PIL.Image.open("config/zara.ico")
    got = sorted(ico.info.get("sizes", []))
    assert got, "no sizes embedded in the .ico"
    print(f"        ico sizes: {got}")
    png = PIL.Image.open("config/zara.png")
    assert png.size == (256, 256), png.size
    assert png.mode == "RGBA", f"no alpha channel: {png.mode}"


@check("the icon round-trips: source portrait regenerates the assets")
def _():
    import PIL.Image
    import ui

    assert ui.ICON_SRC.is_file(), f"icon source missing: {ui.ICON_SRC}"
    before_png = Path("config/zara.png").read_bytes()
    before_ico = Path("config/zara.ico").read_bytes()

    assert ui._build_icons_from_source() is True, "regeneration reported failure"
    assert Path("config/zara.png").read_bytes() == before_png, "png changed"
    assert Path("config/zara.ico").read_bytes() == before_ico, "ico changed"

    im = PIL.Image.open(ui.ICON_SRC)
    print(f"        source: {im.size[0]}x{im.size[1]} {im.mode}")


@check("_app_icon returns a usable QIcon")
def _():
    from PyQt6.QtGui import QIcon
    import ui

    ic = ui._app_icon()
    assert isinstance(ic, QIcon), type(ic)
    assert not ic.isNull(), "_app_icon returned a null icon"
    assert ic.availableSizes(), "_app_icon has no usable sizes"
    print(f"        available sizes: {ic.availableSizes()}")


@check("the window gets the icon, and it survives a rename")
def _():
    import ui

    win = ui.MainWindow(ui.CONFIG_DIR / "nonexistent-face.png")
    # _apply_name_update writes straight to ui.API_FILE, so calling it on the
    # real object rewrites config/api_keys.json. That is exactly how a test run
    # once left the app's own name saved as "Nova" on disk. Point API_FILE at a
    # scratch file for the duration so a test can never touch real settings.
    real_api_file = ui.API_FILE
    with tempfile.TemporaryDirectory() as td:
        ui.API_FILE = Path(td) / "api_keys.json"
        ui.API_FILE.write_text(
            json.dumps({"gemini_api_key": "x", "assistant_name": "Zara",
                        "user_name": "Rustam"}), encoding="utf-8")
        try:
            ic = win.windowIcon()
            assert not ic.isNull(), "MainWindow has no window icon"
            assert ic.availableSizes(), "MainWindow window icon has no sizes"
            win._apply_name_update("Zara", "Rustam")
            assert not win.windowIcon().isNull(), "icon lost after a name change"
            win._apply_name_update("Nova", "Rustam")
            assert not win.windowIcon().isNull(), "icon lost after a rename"
            print(f"        title: {win.windowTitle()!r}")
            assert win._assistant_name == "Nova", win._assistant_name
        finally:
            ui.API_FILE = real_api_file
            win.close()
            win.deleteLater()

    # And prove the real config still says Zara.
    from memory.config_manager import load_api_keys
    real = load_api_keys().get("assistant_name")
    assert real == "Zara", f"a test leaked a name into config: {real!r}"


@check("a user rename is still persisted when the app really does it")
def _():
    from memory import config_manager

    before = config_manager.get_assistant_name()
    try:
        config_manager.save_assistant_config("TestName", "Rustam")
        assert config_manager.get_assistant_name() == "TestName", \
            "the real rename path did not persist"
    finally:
        config_manager.save_assistant_config(before, "Rustam")
    assert config_manager.get_assistant_name() == "Zara", \
        f"restore failed, config says {config_manager.get_assistant_name()!r}"


@check("the app-level icon is set for the taskbar")
def _():
    import ui

    ic = ui._app_icon()
    if ic is not None:
        _APP.setWindowIcon(ic)
    assert not _APP.windowIcon().isNull(), "QApplication has no icon"


@check("no shortcut still points at the old jarvis.ico")
def _():
    src = Path("ui.py").read_text(encoding="utf-8")
    assert "jarvis.ico" not in src, "ui.py still references jarvis.ico"
    # The old shortcut may still be *deleted* — that is the cleanup — but it
    # must never be created again.
    assert 'desktop / "J.A.R.V.I.S.lnk").unlink()' in src, \
        "expected the stale J.A.R.V.I.S.lnk to be cleaned up"
    assert 'lnk      = str(desktop / "J.A.R.V.I.S.lnk")' not in src, \
        "still creates a J.A.R.V.I.S.lnk"
    assert 'J.A.R.V.I.S.app' not in src, "still creates a J.A.R.V.I.S.app"
    assert 'Name=J.A.R.V.I.S' not in src, "still writes a J.A.R.V.I.S .desktop"
    assert "Just A Rather Very Intelligent System" not in src, \
        "still expands the old acronym as a subtitle"


@check("folders get a folder icon and no bogus byte size")
def _():
    import ui

    assert ui._file_category(Path(".")) == "folder", \
        ui._file_category(Path("."))
    assert ui._file_category(Path("x.png")) == "image", \
        ui._file_category(Path("x.png"))
    assert "folder" in ui._FILE_ICONS, "no folder icon in the table"


@check("a dropped folder reaches the model, not a dead end")
def _():
    from PyQt6.QtWidgets import QApplication
    import ui

    app = QApplication.instance() or QApplication([])
    sent = []
    win = ui.MainWindow(ui.CONFIG_DIR / "nonexistent-face.png")
    win.on_text_command = lambda m: sent.append(m)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dropped"
            root.mkdir()
            _tree(root)
            win._on_file_selected(str(root))
        assert sent, "no message reached the model for a dropped folder"
        assert "FOLDER_UPLOADED" in sent[0], sent[0]
        assert str(root) in sent[0], sent[0]
    finally:
        win.close()
        win.deleteLater()


@check("_MODALITY_ERRORS catches the reported error and not tuning drift")
def _():
    import main

    real = ('Cannot read "image.png" (this model does not support image input). '
            "Inform the user.")
    assert main._is_modality_error(real), "the reported error was not recognised"
    assert not main._is_modality_error(
        "RealtimeInputConfig: unknown name \"media_resolution\" at "
        "`session.send_client_content`"), "tuning drift misread as a modality error"
    assert not main._is_modality_error("429 RESOURCE_EXHAUSTED"), "quota misread"


print()
print("=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
print("=" * 62)
for name, tb in FAIL:
    print(f"\n--- {name} ---\n{tb}")
sys.exit(1 if FAIL else 0)
