"""Keeps the Mac awake while an iPhone (or any other API-sourced) upload is
in flight. A phone upload depends on the browser-driven upload loop staying
alive for as long as Shortcuts keeps sending files -- system sleep can
suspend that for hours (see the overnight moto-videos incident), silently
stalling the backup. Runs `caffeinate` as a background subprocess for as
long as at least one "api"-sourced upload is active, and shows a native
notification at start/stop so it's never a silent background effect.

Not used for local (drag-and-drop/watched-folder) uploads -- those happen
while someone's already at the Mac, so the same sleep risk doesn't apply the
same way, and forcing the machine awake for every local drag would be an
unwanted surprise.
"""
import asyncio
import logging

from .photos_automation import _applescript_string

logger = logging.getLogger(__name__)

# A phone-sourced batch arrives as one independent HTTP request per file,
# with real gaps between them (network + on-device processing time) -- the
# active-upload count genuinely hits zero between photos even mid-batch.
# Stopping (and notifying) the instant that happens flapped caffeinate on
# and off, and fired the "keeping awake"/"can sleep" notification pair over
# and over during a single multi-photo backup. Waiting this long after the
# count hits zero before actually stopping collapses that into one true
# start and one true stop per batch -- cancelled if another upload starts
# within the window.
STOP_GRACE_SECONDS = 45

_process: asyncio.subprocess.Process | None = None
_active_count = 0
_pending_stop: asyncio.Task | None = None


async def _notify(message: str) -> None:
    # Quote the message properly even though both current call sites pass
    # fixed literals: this builds AppleScript source, and AppleScript can
    # reach `do shell script`, so the day someone passes a filename (or any
    # other outside string) through here, raw interpolation would turn a
    # notification into arbitrary code execution.
    script = f'display notification {_applescript_string(message)} with title "BackitSnappy"'
    try:
        await asyncio.create_subprocess_exec("osascript", "-e", script)
    except Exception:
        logger.exception("Failed to show keep-awake notification")


async def upload_started() -> None:
    """Call when an "api"-sourced upload begins. Safe to call repeatedly --
    only the first concurrent call actually starts caffeinate, and any
    pending stop (see upload_finished) is cancelled."""
    global _process, _active_count, _pending_stop
    _active_count += 1
    if _pending_stop is not None:
        _pending_stop.cancel()
        _pending_stop = None
    if _process is not None:
        return
    try:
        _process = await asyncio.create_subprocess_exec("caffeinate", "-disu")
    except Exception:
        logger.exception("Failed to start caffeinate")
        _process = None
        return
    await _notify("Keeping your Mac awake while an iPhone backup is in progress.")


async def upload_finished() -> None:
    """Call when an "api"-sourced upload reaches a terminal state (success
    or failure). Once every known upload has settled, schedules the actual
    stop after STOP_GRACE_SECONDS rather than immediately -- see the module
    docstring for why."""
    global _active_count, _pending_stop
    _active_count = max(0, _active_count - 1)
    if _active_count > 0 or _process is None:
        return
    if _pending_stop is None:
        _pending_stop = asyncio.create_task(_stop_after_grace())


async def _stop_after_grace() -> None:
    global _process, _pending_stop
    try:
        await asyncio.sleep(STOP_GRACE_SECONDS)
    except asyncio.CancelledError:
        return
    _pending_stop = None
    if _active_count > 0 or _process is None:
        return
    proc, _process = _process, None
    proc.terminate()
    await proc.wait()
    await _notify("iPhone backup finished — your Mac can sleep again.")


async def shutdown() -> None:
    """Force-stop caffeinate regardless of active count/pending grace timer
    -- called on the app's normal shutdown path so a still-running upload
    doesn't leave the Mac pinned awake after BackitSnappy itself has quit."""
    global _process, _active_count, _pending_stop
    _active_count = 0
    if _pending_stop is not None:
        _pending_stop.cancel()
        _pending_stop = None
    if _process is None:
        return
    proc, _process = _process, None
    proc.terminate()
    await proc.wait()
