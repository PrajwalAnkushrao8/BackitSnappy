"""Low-level bridge to the macOS photo library. No business logic, no
Telegram, no database. Nothing under a .photoslibrary package is ever
touched directly -- every interaction goes through Apple's own supported
interfaces.

Two of those, deliberately, because neither covers the whole job:

- **Reading and exporting** go through Photos.app's scripting interface
  (JXA via `osascript`), which is what exposes `export ... usingOriginals`
  -- the thing that forces an iCloud original to be downloaded rather than
  handing back a placeholder.
- **Deleting** goes through PhotoKit (`PHAssetChangeRequest.deleteAssets:`)
  because Photos' scripting interface simply cannot do it. Its own
  dictionary says so outright: the `delete` command is documented as
  "Only albums and folders can be deleted," and its direct parameter
  accepts exactly the types `album` and `folder`. Passing a media item is
  a type the handler does not accept, which is what produced the
  long-standing `-10000` ("AppleEvent handler failed") error -- not a
  syntax problem, and not fixable by rewriting the script.

The two identify items identically: a Photos scripting `id` and a PhotoKit
`localIdentifier` are the same string (e.g.
"D48F1980-...-7B9FDAD525F4/L0/001"), verified against this library, so ids
captured from one side can be handed straight to the other.

Every osascript call has a bounded timeout: the very first Apple Event this
process ever sends to Photos.app triggers a one-time macOS Automation
permission dialog that blocks until a human answers it (confirmed live
against this app's actual Photos.app). A background poll loop must never be
allowed to hang on that, so a timeout here is read as "still waiting on the
user," not treated as a hard failure.
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PERMISSION_CHECK_TIMEOUT_SECONDS = 5
LIST_ITEMS_TIMEOUT_SECONDS = 60
EXPORT_TIMEOUT_SECONDS = 300
DELETE_TIMEOUT_SECONDS = 30

# The Apple Event error for "not authorized to send Apple events to that
# app" -- distinguishes an explicit prior denial from an as-yet-unanswered
# permission prompt (which just hangs instead of erroring).
_NOT_AUTHORIZED_ERROR_NUMBER = "-1743"

PermissionStatus = Literal["granted", "denied", "undetermined", "error"]


class PhotosAutomationError(Exception):
    pass


async def _run_osascript(script: str, timeout_seconds: float, language: str | None = "JavaScript") -> str:
    """Runs a script via osascript (JXA by default, or plain AppleScript
    when language=None) and returns its stdout, stripped. Raises
    asyncio.TimeoutError (still running, presumably blocked on an
    unanswered permission prompt) or PhotosAutomationError (osascript ran
    and reported a real failure)."""
    args = ["osascript"]
    if language:
        args += ["-l", language]
    args += ["-e", script]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise
    if proc.returncode != 0:
        raise PhotosAutomationError((stderr or stdout).decode(errors="replace").strip())
    return stdout.decode(errors="replace").strip()


async def _run_jxa(script: str, timeout_seconds: float) -> str:
    return await _run_osascript(script, timeout_seconds, language="JavaScript")


def _applescript_string(s: str) -> str:
    """Safely embeds an arbitrary string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def check_permission(timeout_seconds: float = PERMISSION_CHECK_TIMEOUT_SECONDS) -> PermissionStatus:
    """Read-only probe: a trivial Apple Event to Photos.app. Never called
    from a context that can't afford to wait out timeout_seconds."""
    script = """
    (function () {
      try {
        var app = Application("Photos");
        app.mediaItems.length;
        return "granted";
      } catch (e) {
        return "denied:" + (e.errorNumber || "") + ":" + e.message;
      }
    })();
    """
    try:
        result = await _run_jxa(script, timeout_seconds)
    except asyncio.TimeoutError:
        return "undetermined"
    except PhotosAutomationError as exc:
        logger.warning("Photos permission check failed: %s", exc)
        return "error"
    if result.startswith("granted"):
        return "granted"
    if _NOT_AUTHORIZED_ERROR_NUMBER in result:
        return "denied"
    logger.warning("Unexpected Photos permission check result: %s", result)
    return "error"


async def list_all_item_ids() -> list[tuple[str, str]]:
    """[(photos_item_id, filename), ...] for every media item in the
    library, fetched via one bulk plural-property Apple Event rather than
    one round-trip per item (Photos' scripting bridge is slow enough that
    per-item access doesn't scale to real libraries)."""
    script = """
    (function () {
      var app = Application("Photos");
      var items = app.mediaItems;
      var ids = items.id();
      var names = items.filename();
      var out = [];
      for (var i = 0; i < ids.length; i++) {
        out.push([ids[i], names[i]]);
      }
      return JSON.stringify(out);
    })();
    """
    raw = await _run_jxa(script, LIST_ITEMS_TIMEOUT_SECONDS)
    return [tuple(pair) for pair in json.loads(raw)]


async def export_item(item_id: str, dest_dir: Path) -> list[Path]:
    """Exports one item's full-resolution original(s) into dest_dir (must
    already exist and be empty) -- this forces an iCloud download if the
    original isn't cached locally yet, the same guarantee Shortcuts gives.
    Usually one file, but a Live Photo's original is genuinely two files
    (the still HEIC and its paired motion .mov) -- returns whatever
    actually landed rather than assuming a count or name, since Photos
    picks both on its own."""
    script = f"""
    (function () {{
      var app = Application("Photos");
      var target = app.mediaItems.whose({{id: {json.dumps(item_id)}}})[0];
      app.export([target], {{to: Path({json.dumps(str(dest_dir))}), usingOriginals: true}});
      return "ok";
    }})();
    """
    await _run_jxa(script, EXPORT_TIMEOUT_SECONDS)
    exported = [p for p in dest_dir.iterdir() if p.is_file()]
    if not exported:
        raise PhotosAutomationError(f"Export produced no files for item {item_id}")
    return exported


def photokit_authorization_status() -> str:
    """Read-write PhotoKit authorization, as a plain string. Distinct from
    check_permission() above, which reports the *Automation* (Apple Events)
    permission that export/list need -- deleting needs this one instead,
    and macOS grants them separately."""
    import Photos

    status = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(Photos.PHAccessLevelReadWrite)
    return {
        0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized", 4: "limited",
    }.get(status, f"unknown({status})")


def _delete_assets_blocking(item_ids: list[str]) -> int:
    """Synchronous PhotoKit deletion -- see delete_items for why this is
    batched and what the caller must know about the confirmation prompt."""
    import Photos

    fetched = Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(list(item_ids), None)
    if fetched is None or fetched.count() == 0:
        return 0
    assets = [fetched.objectAtIndex_(i) for i in range(fetched.count())]

    def changes() -> None:
        Photos.PHAssetChangeRequest.deleteAssets_(assets)

    library = Photos.PHPhotoLibrary.sharedPhotoLibrary()
    ok, error = library.performChangesAndWait_error_(changes, None)
    if not ok:
        raise PhotosAutomationError(f"PhotoKit deletion failed: {error}")
    return len(assets)


async def delete_items(item_ids: list[str]) -> int:
    """Moves items to Photos' own Recently Deleted (Apple's 30-day window)
    -- never a raw file delete. Only ever called for items with a confirmed
    Telegram upload behind them. Returns how many assets were actually
    deleted, which can be fewer than requested if some ids no longer
    resolve (already removed by hand, say).

    Takes a *list*, and callers should pass the whole batch at once rather
    than looping: macOS shows the user one confirmation prompt per change
    request, so a batch of 139 is one prompt, while 139 single-item calls
    would be 139 prompts. That prompt is enforced by the system for an
    unbundled process like this one and cannot be suppressed -- it is the
    reason this whole feature is inherently attended rather than silent.

    Runs in a worker thread: performChangesAndWait_ blocks until the user
    answers that prompt, which must not stall the event loop everything
    else in the app shares."""
    if not item_ids:
        return 0
    return await asyncio.to_thread(_delete_assets_blocking, item_ids)
