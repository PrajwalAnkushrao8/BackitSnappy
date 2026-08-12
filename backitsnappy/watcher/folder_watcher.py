"""Watches a chosen folder for new files and uploads them once they're fully
written.

macOS's FSEvents backend (which watchdog uses here) has no reliable "file
closed" signal — on_closed/FileClosedEvent is Linux-inotify-only — so
completion is detected by polling size/mtime until they stop changing across
a few consecutive checks. This avoids uploading a partial file mid-copy.
"""
import logging
import os
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
STABLE_POLLS_REQUIRED = 3


class _PendingFile:
    __slots__ = ("size", "mtime", "stable_count")

    def __init__(self, size: int, mtime: float):
        self.size = size
        self.mtime = mtime
        self.stable_count = 0


class _Handler(FileSystemEventHandler):
    def __init__(self, pending: dict, lock: threading.Lock):
        self._pending = pending
        self._lock = lock

    def _touch(self, path: str) -> None:
        p = Path(path)
        if p.name.startswith("."):
            return
        try:
            if not p.is_file():
                return
            stat = p.stat()
        except OSError:
            return
        with self._lock:
            self._pending[path] = _PendingFile(stat.st_size, stat.st_mtime)

    def on_created(self, event):
        if not event.is_directory:
            self._touch(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._touch(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._touch(event.src_path)


class FolderWatcher:
    def __init__(self, folder: str, on_stable_file):
        """on_stable_file: callable(path: str) invoked once a file under
        `folder` has finished being written."""
        self._folder = folder
        self._on_stable_file = on_stable_file
        self._pending: dict[str, _PendingFile] = {}
        self._lock = threading.Lock()
        self._observer = Observer()
        self._observer.schedule(_Handler(self._pending, self._lock), folder, recursive=True)
        self._stop_event = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self) -> None:
        self._observer.start()
        self._poll_thread.start()
        logger.info("Watching folder: %s", self._folder)

    def stop(self) -> None:
        self._stop_event.set()
        self._observer.stop()
        self._observer.join(timeout=5)

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(POLL_INTERVAL_SECONDS):
            self._check_pending()

    def _check_pending(self) -> None:
        ready = []
        with self._lock:
            for path, info in list(self._pending.items()):
                try:
                    stat = os.stat(path)
                except OSError:
                    del self._pending[path]
                    continue
                if stat.st_size == info.size and stat.st_mtime == info.mtime:
                    info.stable_count += 1
                else:
                    info.size = stat.st_size
                    info.mtime = stat.st_mtime
                    info.stable_count = 0
                if info.stable_count >= STABLE_POLLS_REQUIRED:
                    ready.append(path)
                    del self._pending[path]
        for path in ready:
            try:
                self._on_stable_file(path)
            except Exception:
                logger.exception("on_stable_file callback failed for %s", path)


class WatcherController:
    """Owns at most one active FolderWatcher and lets it be swapped safely
    when the user changes the watched folder in Settings, without an app
    restart."""

    def __init__(self, on_stable_file):
        self._on_stable_file = on_stable_file
        self._watcher: FolderWatcher | None = None
        self._lock = threading.Lock()

    def restart(self, folder: str | None) -> None:
        with self._lock:
            if self._watcher is not None:
                self._watcher.stop()
                self._watcher = None
            if folder:
                try:
                    watcher = FolderWatcher(folder, self._on_stable_file)
                    watcher.start()
                    self._watcher = watcher
                except OSError:
                    logger.exception("Failed to start watcher for folder: %s", folder)
                    raise

    def stop(self) -> None:
        self.restart(None)
