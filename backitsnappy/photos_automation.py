"""Low-level bridge to macOS Photos.app via JXA (JavaScript for Automation),
run through `osascript`. This module is scripting-only -- no business logic,
no Telegram, no database. It never touches anything under a .photoslibrary
package directly; every interaction goes through Photos' own scripting
interface (`export`, `delete`), the same guarantee Apple's own Shortcuts
integration relies on.

Every call has a bounded timeout: the very first Apple Event this process
ever sends to Photos.app triggers a one-time macOS Automation permission
dialog that blocks until a human answers it (confirmed live against this
app's actual Photos.app -- see the plan). A background poll loop must never
be allowed to hang on that, so a timeout here is read as "still waiting on
the user," not treated as a hard failure.
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


async def delete_item(item_id: str) -> None:
    """Moves one item to Photos' own Recently Deleted -- never a raw file
    delete. Only ever called after a confirmed Telegram upload.

    Plain AppleScript rather than JXA here, unlike every other call in this
    module: two different JXA argument shapes for delete() each failed
    differently (a type-coercion error, then a generic internal handler
    failure) even though export/list -- which touch the same mediaItems
    collection -- work fine in JXA. That pattern points at JXA's bridge
    translation for Photos' delete command specifically, not at argument
    shape. AppleScript is what Photos' scripting dictionary was actually
    authored against, so it sidesteps the bridge-translation question
    entirely rather than trying a third JXA guess."""
    script = f"""
    tell application "Photos"
        delete (media item id {_applescript_string(item_id)})
    end tell
    """
    await _run_osascript(script, DELETE_TIMEOUT_SECONDS, language=None)
