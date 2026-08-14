"""Entrypoint.

pywebview's GUI loop must run on the main thread on macOS, so a background
thread owns its own asyncio event loop hosting the Telethon client and the
FastAPI/uvicorn servers. On window close, a shutdown handshake stops the
servers and disconnects Telethon before the process exits.
"""
import asyncio
import logging
import threading
from pathlib import Path

import pillow_heif

from . import config, db, keepawake
from .api.server import create_app, run_servers
from .telegram.auth_flow import AuthState
from .telegram.client_manager import TelegramManager
from .ui.window import create_window
from .ui.window import start as start_webview
from .watcher.folder_watcher import WatcherController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

pillow_heif.register_heif_opener()  # so Pillow can thumbnail iPhone HEIC photos


def _resume_pending_uploads(manager: TelegramManager) -> None:
    """Anything still in upload_queue means the app closed or crashed
    before that upload reached a terminal state last session -- pick up
    where it left off rather than losing track of it silently."""
    for row in db.get_pending_uploads():
        temp_path = Path(row["temp_path"])
        if not temp_path.exists():
            logger.warning("Queued upload %s is missing on disk, dropping it", temp_path)
            db.dequeue_upload(row["id"])
            continue
        logger.info("Resuming interrupted upload: %s", temp_path)
        manager.start_upload(
            temp_path, album_id=row["album_id"], source=row["source"], delete_after=True, queue_id=row["id"],
        )


def _run_background(
    loop: asyncio.AbstractEventLoop,
    local_ready: threading.Event,
    stop_event_holder: dict,
) -> None:
    asyncio.set_event_loop(loop)

    async def _main() -> None:
        db.init_db()
        manager = TelegramManager(loop)
        await manager.start()
        if manager.state == AuthState.AUTHORIZED:
            _resume_pending_uploads(manager)

        app = create_app(manager)

        def on_stable_file(path: str) -> None:
            manager.queue_upload_threadsafe(path, source="watcher")

        watcher_controller = WatcherController(on_stable_file)
        app.state.watcher_controller = watcher_controller
        watch_folder = config.get("watch_folder")
        if watch_folder:
            try:
                watcher_controller.restart(watch_folder)
            except OSError:
                logger.exception("Could not start folder watcher at startup")

        stop_event = asyncio.Event()
        stop_event_holder["event"] = stop_event

        await run_servers(app, stop_event, on_local_ready=local_ready.set)

        watcher_controller.stop()
        await keepawake.shutdown()
        await manager.disconnect()

    loop.run_until_complete(_main())


def main() -> None:
    loop = asyncio.new_event_loop()
    local_ready = threading.Event()
    stop_event_holder: dict = {}

    thread = threading.Thread(
        target=_run_background, args=(loop, local_ready, stop_event_holder), daemon=True
    )
    thread.start()
    local_ready.wait()

    cfg = config.load()
    url = f"http://127.0.0.1:{cfg['local_port']}/"

    def on_closed() -> None:
        loop.call_soon_threadsafe(stop_event_holder["event"].set)
        thread.join(timeout=10)

    create_window(url, on_closed)
    start_webview()


if __name__ == "__main__":
    main()
