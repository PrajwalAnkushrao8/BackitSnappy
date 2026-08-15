"""pywebview window setup and the local JS-API bridge.

The bridge (js_api) is an in-process channel between the native webview and
this Python process — never a network socket — so it's the one place the
pairing token can be handed to the frontend without ever putting it on the
wire, even to the loopback listener.
"""
import logging
import shutil
import tempfile
import webbrowser
from pathlib import Path

import webview

from .. import secrets_store

logger = logging.getLogger(__name__)

ICON_PATH = Path(__file__).resolve().parent / "static" / "icons" / "logo-1024.png"


class JSBridge:
    def __init__(self) -> None:
        self.window: webview.Window | None = None

    def get_pairing_token(self) -> str:
        return secrets_store.get_pairing_token()

    def open_telegram_api_page(self) -> None:
        """Opens my.telegram.org/apps in the system's default browser (not
        inside the app's own webview -- that page needs a real login flow
        and shouldn't be attempted inside the embedded WKWebView). A
        dedicated method rather than a generic open_url(url) so the bridge
        can't be used to launch an arbitrary URL from the frontend."""
        webbrowser.open("https://my.telegram.org/apps")

    def get_free_disk_space_bytes(self) -> int:
        """Free space on the volume uploads are temp-staged on (same path
        as routes_upload.py's UPLOAD_TMP_DIR) -- used for a pre-flight
        warning before large batches, since the browser has no API for
        real host disk space."""
        return shutil.disk_usage(tempfile.gettempdir()).free

    def open_automation_settings(self) -> None:
        """Opens System Settings -> Privacy & Security -> Automation, for
        the "grant BackitSnappy permission to control Photos" flow. Same
        dedicated-method rationale as open_telegram_api_page."""
        webbrowser.open("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")

    def save_file_dialog(self, default_filename: str) -> str | None:
        """Native macOS "Save As" panel. pywebview runs every js_api call on
        its own throwaway thread (not the main/Cocoa thread, not the
        Telethon event-loop thread), so this blocking modal call is safe to
        make directly — see the Storage-gallery plan's validation notes."""
        result = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(Path.home() / "Downloads"),
            save_filename=default_filename,
        )
        if not result:
            return None
        return result[0]


def _set_dock_icon() -> None:
    """pywebview's Cocoa backend doesn't support the `icon` param (that's
    GTK/QT only -- see webview/__init__.py's own docstring), so the dock
    icon needs a direct PyObjC call. Best-effort: PyObjC is already a
    pywebview dependency on macOS, but if this fails for any reason the app
    should still launch fine with the default interpreter icon rather than
    fail to start over cosmetics."""
    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initWithContentsOfFile_(str(ICON_PATH))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        logger.exception("Failed to set dock icon")


def _allow_video_autoplay() -> None:
    """pywebview's macOS backend leaves WKWebViewConfiguration at its
    default media-autoplay policy, which blocks even a muted video.play()
    call made synchronously from within a real click handler -- unlike a
    normal Safari/Chrome tab, where that's exactly the standard workaround
    and works fine. Confirmed by testing: the lightbox's video element
    still stayed paused despite an explicit, gesture-scoped play() call
    (see album-gallery.js). pywebview has no public setting for this (only
    ALLOW_FILE_URLS/ALLOW_DOWNLOADS/etc — see its `settings` dict), so this
    patches its cocoa backend to relax the policy on the same live
    WKWebViewConfiguration object it already mutates itself a few lines
    later for its own preferences (see BrowserView.__init__ in
    webview/platforms/cocoa.py) -- right after each window's WKWebView is
    constructed, before any page loads into it. Best-effort: if pywebview's
    internals change shape in a future version, this just silently stops
    applying rather than breaking window creation."""
    try:
        from webview.platforms import cocoa
    except ImportError:
        return
    if getattr(cocoa.BrowserView, "_backitsnappy_autoplay_patched", False):
        return
    original_init = cocoa.BrowserView.__init__

    def patched_init(self, window):
        original_init(self, window)
        try:
            self.webview.configuration().setMediaTypesRequiringUserActionForPlayback_(0)
        except Exception:
            logger.exception("Failed to relax WKWebView autoplay policy")

    cocoa.BrowserView.__init__ = patched_init
    cocoa.BrowserView._backitsnappy_autoplay_patched = True


def create_window(url: str, on_closed) -> webview.Window:
    _set_dock_icon()
    _allow_video_autoplay()
    bridge = JSBridge()
    window = webview.create_window(
        "BackitSnappy",
        url=url,
        js_api=bridge,
        width=1100,
        height=720,
        min_size=(800, 560),
    )
    bridge.window = window
    window.events.closed += on_closed
    return window


def start(debug: bool = False) -> None:
    webview.start(debug=debug)
