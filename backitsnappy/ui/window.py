"""pywebview window setup and the local JS-API bridge.

The bridge (js_api) is an in-process channel between the native webview and
this Python process — never a network socket — so it's the one place the
pairing token can be handed to the frontend without ever putting it on the
wire, even to the loopback listener.
"""
import webview

from .. import secrets_store


class JSBridge:
    def get_pairing_token(self) -> str:
        return secrets_store.get_pairing_token()

    def rotate_pairing_token(self) -> str:
        return secrets_store.rotate_pairing_token()


def create_window(url: str, on_closed) -> webview.Window:
    window = webview.create_window(
        "BackitSnappy",
        url=url,
        js_api=JSBridge(),
        width=1100,
        height=720,
        min_size=(800, 560),
    )
    window.events.closed += on_closed
    return window


def start(debug: bool = False) -> None:
    webview.start(debug=debug)
