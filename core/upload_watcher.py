"""A folder Zara watches, so a file can be handed to her by dropping it there.

The drop zone on the HUD is the obvious way to give Zara a file, but it needs
the app in front of you. This is the other way: put a file in the upload folder
and, while Zara is running, she notices it, reads it, and says what is in it —
no clicking, no dragging, and it works from Explorer while she is doing
something else.

WHY POLLING AND NOT watchdog/watchfiles
    Both are available or installable, and both are better at noticing a change.
    Neither is better at the part that actually matters here. Every one of these
    events fires the moment a file is *created* — which is the instant a copy
    has written its first byte, not the instant it is readable. A watcher
    therefore hands a half-written JPEG to the image decoder and gets a
    truncated-file error, or worse, a valid-looking half an image. Deciding a
    file has finished arriving is a separate question from being told it
    appeared, and it is answered here by watching the size stop changing. Since
    that loop has to exist either way, the event source in front of it is
    chosen for having no dependency: `pip install watchdog` failing on someone
    else's machine should not disable a feature they use every day.

    The trade is honest — a change is noticed up to `poll_interval` late, about
    a second. For dropping a file on your way out of the room, that is
    invisible.

A file is considered ready when its size has been unchanged for `stable_secs`.
Anything still growing is left alone and re-checked, which is what makes a
multi-gigabyte copy safe to leave in the folder: Zara starts on it when it
lands, not while it is arriving.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

# Written by a copy that has not finished. A browser downloading into the
# folder leaves one of these behind the whole time, and its companion file has
# the real name — so reading it is both useless and wrong.
PARTIAL_SUFFIXES = (
    ".crdownload", ".part", ".partial", ".tmp", ".temp", ".download",
    ".filepart", ".!ut", ".opdownload",
)
PARTIAL_PREFIXES = ("~$", ".~", "._", ".goutputstream")

# A still-open handle on Windows means someone is writing to it right now.
# os.open is the only portable way to ask; the file is closed again immediately.
_STABLE_POLL = 0.4


def is_partial(name: str) -> bool:
    """True for a file that is mid-copy and must not be read yet."""
    n = name.lower()
    if any(n.startswith(p.lower()) for p in PARTIAL_PREFIXES):
        return True
    return n.endswith(PARTIAL_SUFFIXES)


def _is_writable_by_other(fp: Path) -> bool:
    """True when something else still holds the file open for writing."""
    if os.name != "nt":
        return False
    try:
        import msvcrt
        fd = os.open(str(fp), os.O_RDONLY)
    except OSError:
        return True                      # cannot even open it: not ready
    try:
        # SHaring check: msvcrt.locking on a region the writer holds fails.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


class UploadWatcher:
    """Watches one folder and calls `on_file(path)` for each new arrival.

    Threading contract: the watcher thread never raises into its caller, never
    touches Qt, and stops within `stop_timeout` of `stop()`. The callback runs
    on the watcher's thread, so it must marshal to the UI itself if it needs to
    touch widgets — the app's own `on_text_command` does exactly that with
    `run_coroutine_threadsafe`.
    """

    def __init__(self, folder, on_file, poll_interval: float = 1.0,
                 stable_secs: float = 1.5, log=None):
        self.folder = Path(folder)
        self.on_file = on_file
        self.poll_interval = float(poll_interval)
        self.stable_secs = float(stable_secs)
        self._log = log or (lambda *_: None)

        # path -> (size, mtime, first_seen_monotonic, last_change_monotonic)
        self._seen: dict[Path, tuple] = {}
        self._done: set[Path] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Guards the two sets above. Only the watcher thread mutates them, but
        # `pending()` and `forget()` are called from elsewhere, and a dict
        # resizing while another thread iterates it is a real crash, not a
        # warning.
        self._lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def ensure_folder(self) -> bool:
        """Create the folder if it is missing. A watch folder that does not
        exist yet is the normal first-run state, not an error."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as e:
            self._log(f"ERR: Could not create the upload folder "
                      f"{self.folder} — {e}")
            return False

    def start(self) -> bool:
        if not self.ensure_folder():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="zara-upload-watcher", daemon=True)
        self._thread.start()
        self._log(f"SYS: Watching {self.folder} for new files.")
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── public helpers ─────────────────────────────────────────────────────

    def forget(self, path) -> None:
        """Let a path be picked up again — after the user deletes the result, or
        asks for the same file to be re-read."""
        p = Path(path)
        with self._lock:
            self._seen.pop(p, None)
            self._done.discard(p)

    def pending(self) -> list[Path]:
        """Files noticed but not yet handed over. Exposed so the UI can show
        'reading…' instead of looking broken while a big copy settles."""
        with self._lock:
            return sorted(p for p in self._seen if p not in self._done)

    def processed(self) -> set[Path]:
        with self._lock:
            return set(self._done)

    # ── the loop ───────────────────────────────────────────────────────────

    def _candidates(self):
        """Top-level regular files only. A subfolder dropped in here is left
        alone on purpose: ingesting a whole tree the user did not ask for is
        worse than asking them to drop the folder on the HUD, which is the
        explicit gesture that means "read all of this"."""
        try:
            entries = list(self.folder.iterdir())
        except OSError:
            return []
        out = []
        for p in entries:
            try:
                if p.is_file() and not is_partial(p.name):
                    out.append(p)
            except OSError:
                continue
        return out

    def _run(self) -> None:
        # A first pass would treat every file already sitting in the folder as
        # a new arrival and process the whole backlog on startup. Existing files
        # are seeded into _done instead, so only what arrives *after* launch is
        # picked up. `seed_existing=False` opts into reading the backlog.
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:            # never let the thread die
                self._log(f"ERR: Upload watcher — {type(e).__name__}: {e}")
            self._stop.wait(self.poll_interval)

    def seed_existing(self) -> int:
        """Mark what is already in the folder as seen. Returns the count."""
        n = 0
        for p in self._candidates():
            with self._lock:
                self._done.add(p)
            n += 1
        if n:
            self._log(f"SYS: {n} existing file(s) in the upload folder were "
                      f"left alone — only new arrivals are read.")
        return n

    def _tick(self) -> None:
        now = time.monotonic()
        for p in self._candidates():
            try:
                st = p.stat()
            except OSError:
                continue
            size, mtime = st.st_size, st.st_mtime

            with self._lock:
                if p in self._done:
                    continue
                prev = self._seen.get(p)
                if prev is None or prev[0] != size or prev[1] != mtime:
                    # New, or still changing. Reset the quiet timer.
                    self._seen[p] = (size, mtime, now, now)
                    continue
                if now - prev[3] < self.stable_secs:
                    continue
                if size == 0:
                    # Zero bytes and stable: an empty file is a real thing, but
                    # for a drop folder it is far more often a placeholder left
                    # by a sync client. Mark it done so it is not retried
                    # forever, and say why.
                    self._done.add(p)
                    self._seen.pop(p, None)
                    self._log(f"ERR: '{p.name}' is empty (0 bytes) — skipped.")
                    continue
                self._done.add(p)
                self._seen.pop(p, None)

            if _is_writable_by_other(p):
                # Still locked, so not actually finished. Un-mark it and let
                # the next tick try again.
                with self._lock:
                    self._done.discard(p)
                    self._seen[p] = (size, mtime, now, now)
                continue

            self._deliver(p)

    def _deliver(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        self._log(f"SYS: New file — {path.name} ({size:,} bytes)")
        try:
            self.on_file(path)
        except Exception as e:
            # A file Zara cannot read must not stop the watcher; the next one
            # dropped in should still work.
            self._log(f"ERR: Could not read {path.name} — "
                      f"{type(e).__name__}: {e}")

    # ── test / manual use ──────────────────────────────────────────────────

    def poll_once(self) -> list[Path]:
        """Run a single tick and report what it delivered. Used by the tests, and
        by anything that wants to check the folder without a thread."""
        before = self.processed()
        self._tick()
        return sorted(self.processed() - before)
