"""Media-serving routes (thumbnails, full media for the lightbox/video
playback, and range-based streaming straight from Telegram). Mounted with
the flexible (header-or-query-token) auth dependency in server.py, since
browsers can't attach custom headers to <img>/<video> src requests — every
other route stays header-only.
"""
import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.responses import FileResponse

from .. import db, media
from ..telegram.client_manager import TelegramManager
from .deps import get_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# How many bytes to request from Telegram per internal chunk while
# streaming -- independent of how big a range the browser actually asked
# for (that's enforced in the generator below by trimming/stopping). 256KB
# rather than a larger "more efficient" value: 512KB (524288) empirically
# triggers Telegram's LimitInvalidError at some offsets on real files --
# reproduced directly against the live API -- while 64KB/128KB/256KB/1MB
# all succeeded at that exact offset, across dozens of random offsets.
# 256KB (not 64KB) specifically to cut the number of sequential
# round-trips 4x for high-bitrate video, where buffering enough seconds of
# playback ahead needs real throughput, not just a fast first byte.
STREAM_REQUEST_SIZE = 256 * 1024
# How many chunks to keep in flight at once -- fetched concurrently rather
# than one at a time, since per-chunk latency (not bandwidth) is the real
# bottleneck under load: overlapping ~2-6s round-trips finish in roughly
# the time one does, instead of stacking sequentially. GetFileRequest is
# its own flood-wait bucket, and this window applies per Range request --
# a single video element commonly has more than one Range request in
# flight at once (an initial probe plus the real playback fetch, or a seek
# landing while the previous buffer-ahead fetch is still running), so the
# real concurrent request rate against Telegram for one open video can be
# a multiple of this number, not a hard ceiling. If this starts producing
# FloodWaitErrors in practice, turn it back down -- there's no way to
# verify Telegram's actual threshold without hitting it live.
STREAM_CONCURRENCY = 8
# Cap on how much of a video's beginning gets captured for an opportunistic
# thumbnail (see stream_media) -- generous enough to likely contain a
# decodable keyframe for videos whose metadata sits near the start, without
# mattering bandwidth-wise on top of what's already being streamed for
# playback (this capture costs zero *extra* requests -- it's a copy of
# bytes already in flight, not a separate download).
THUMBNAIL_CAPTURE_BYTES = 8 * 1024 * 1024
# Both forms are valid HTTP Range syntax: "bytes=START-END"/"bytes=START-"
# (group 1 set) and the suffix form "bytes=-N" meaning "the last N bytes"
# (group 1 empty, group 2 set) -- QuickTime .mov players in particular
# commonly probe with a suffix range on their very first request, before
# they've learned the file's total size from a response, to locate
# trailing metadata (the moov atom often sits at the end of camera-
# original footage). Rejecting that form outright was a real gap.
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


# GetFileRequest intermittently raises LimitInvalidError on a perfectly
# valid (request_size, offset) pair -- confirmed by direct testing earlier
# (reproducible even at the same offset/size that succeeds moments later)
# and confirmed again live: running several of these concurrently for the
# same file makes it noticeably more common, most likely just more total
# requests landing close together on whatever's flaky Telegram-side, not a
# new failure mode of its own. A bare retry of the identical call reliably
# clears it in practice, so that's the fix -- not chasing a "correct"
# request_size, since 65536/131072/262144/1048576 have all been observed
# both failing and succeeding at one point or another.
FETCH_MAX_ATTEMPTS = 4
FETCH_RETRY_DELAY_SECONDS = 0.6


async def _fetch_chunk(manager: TelegramManager, message, offset: int) -> bytes:
    """Fetches exactly one STREAM_REQUEST_SIZE-ish chunk at offset -- a
    single-shot iter_download rather than the streaming multi-chunk loop
    used elsewhere, so the caller can launch several of these concurrently
    at different offsets and await them in order (see stream_media)."""
    last_exc: Exception | None = None
    for attempt in range(FETCH_MAX_ATTEMPTS):
        try:
            async for chunk in manager.client.iter_download(
                message, offset=offset, request_size=STREAM_REQUEST_SIZE, limit=1
            ):
                return bytes(chunk)
            return b""
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Chunk fetch at offset %d failed (attempt %d/%d): %s",
                offset, attempt + 1, FETCH_MAX_ATTEMPTS, exc,
            )
            if attempt + 1 < FETCH_MAX_ATTEMPTS:
                await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS)
    raise last_exc


async def _finish_thumbnail_capture(manager: TelegramManager, file_row, data: bytes) -> None:
    """Runs in the background once stream_media's body() generator has
    finished (or been cut short by an early client disconnect) -- builds a
    thumbnail from whatever head-of-file bytes got captured along the way,
    at zero extra network cost since those bytes were already fetched for
    playback. Best-effort: videos whose metadata sits at the end of the
    file (typical of unprocessed iPhone camera footage) simply won't
    produce a thumbnail from a head-only capture, same limitation the old
    dedicated probe had -- ffmpeg fails cleanly on that, which
    media.generate_thumbnail already treats as "unavailable", not an
    error. Always releases the in-flight guard on the way out -- see
    claim_thumbnail_probe -- so a failed attempt (most often the tiny
    metadata-probe request that precedes real playback, with barely any
    bytes captured) doesn't permanently block the real request right
    behind it from getting its own, much better-supplied attempt."""
    try:
        if not data:
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="backitsnappy-thumb-capture-"))
        try:
            # safe_filename: this name is Telegram-supplied, and this sink
            # runs automatically on first stream (no user action), so an
            # unsanitized name here writes attacker-controlled bytes to an
            # arbitrary path.
            tmp_path = tmp_dir / media.safe_filename(file_row["filename"])
            await asyncio.to_thread(tmp_path.write_bytes, data)
            await asyncio.to_thread(
                media.generate_thumbnail, tmp_path, file_row["sha256_hash"], file_row["mime_type"]
            )
        except Exception:
            logger.exception("Opportunistic thumbnail capture failed for file %s", file_row["id"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        manager.release_thumbnail_probe(file_row["id"])


@router.get("/{file_id}/thumbnail")
async def get_thumbnail(file_id: int):
    file_row = db.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    path = media.thumbnail_path(file_row["sha256_hash"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="No thumbnail available")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{file_id}/media")
async def get_media(file_id: int):
    """Serves the full file for lightbox viewing/video playback, with
    Range-request support (FileResponse handles this automatically). The
    caller must have already called POST /{file_id}/prepare and polled it
    to completion — this route only serves what's already cached locally,
    it never triggers a Telegram download itself."""
    file_row = db.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    path = media.cached_media_path(file_row["sha256_hash"], file_row["filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not downloaded yet -- call /prepare first")
    return FileResponse(path, media_type=file_row["mime_type"] or "application/octet-stream")


@router.get("/{file_id}/stream")
async def stream_media(file_id: int, request: Request, manager: TelegramManager = Depends(get_manager)):
    """Streams straight from Telegram using ranged reads (the same
    mechanism the partial-hash dedup fingerprint uses), so video playback
    can start immediately without downloading -- or permanently caching --
    the whole file first. Never writes to media_cache; Download/Save As
    still go through /prepare + /media for a real local copy."""
    file_row = db.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")

    size = file_row["size"]
    start, end = 0, size - 1
    status_code = 200
    range_header = request.headers.get("range")
    if range_header:
        match = _RANGE_RE.match(range_header)
        if not match or not (match.group(1) or match.group(2)):
            raise HTTPException(status_code=416, detail="Invalid Range header")
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:
            # Suffix range ("bytes=-N") -- the last N bytes of the file.
            suffix_len = int(match.group(2))
            start = max(0, size - suffix_len)
            end = size - 1
        status_code = 206
    if start > end or start >= size:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable")

    try:
        message = await manager.resolve_message(file_row)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    length = end - start + 1

    # Opportunistic thumbnail capture only makes sense for a request that
    # actually starts at the true beginning of the file (how real playback
    # starts) -- a mid-file seek's bytes wouldn't help ffmpeg find a
    # keyframe near the start anyway. claim_thumbnail_probe ensures only
    # one concurrent/future request for this file ever captures, even if
    # the video is reopened many times in one app session.
    want_thumbnail = (
        start == 0
        and media.classify(file_row["mime_type"]) == "video"
        and not media.thumbnail_path(file_row["sha256_hash"]).exists()
        and manager.claim_thumbnail_probe(file_id)
    )

    async def body():
        remaining = length
        capture = bytearray() if want_thumbnail else None

        # request_size must stay a fixed, Telegram-valid value (a power of
        # two from 4KB-1MB) regardless of how few bytes remain -- shrinking
        # it to match `remaining` produces an arbitrary size like 100000
        # that Telegram's GetFileRequest rejects outright
        # (LimitInvalidError). Over-fetching the tail end of a small range
        # is fine; it's trimmed below instead of requested away.
        #
        # Every Telegram request offset must itself be a multiple of
        # request_size -- confirmed directly live: offset 61669376 (not a
        # multiple of 262144) reliably raised LimitInvalidError on a real
        # file, while the very same request aligned down to 61603840
        # (still request_size=262144) succeeded immediately. `start` is
        # whatever arbitrary byte a browser Range-requested (a seek lands
        # wherever the user dragged to), so it can't be assumed aligned --
        # fetch from the next lower aligned offset instead and trim the
        # extra leading_trim bytes off the first chunk before it's ever
        # sent to the client, rather than requesting the exact unaligned
        # offset and hoping Telegram accepts it.
        aligned_start = (start // STREAM_REQUEST_SIZE) * STREAM_REQUEST_SIZE
        leading_trim = start - aligned_start
        offsets = list(range(aligned_start, start + length, STREAM_REQUEST_SIZE))
        first_chunk = True

        # Sliding window: up to STREAM_CONCURRENCY chunks in flight at
        # once (per-chunk latency, not bandwidth, is the real bottleneck --
        # see STREAM_CONCURRENCY above), but always yielded to the client
        # strictly in offset order regardless of which finishes first, so
        # the response body stays byte-correct. The initial burst is
        # staggered a little (not launched in the same instant) -- seen
        # live to reduce how often GetFileRequest's flaky LimitInvalidError
        # (see _fetch_chunk) shows up, presumably because it's less likely
        # when the same file doesn't have a dozen simultaneous requests
        # landing in the same instant. Negligible cost either way against
        # the multi-second per-chunk latency this whole window exists to
        # hide.
        window = []
        for i, o in enumerate(offsets[:STREAM_CONCURRENCY]):
            if i:
                await asyncio.sleep(0.05)
            window.append(asyncio.create_task(_fetch_chunk(manager, message, o)))
        next_idx = len(window)
        try:
            while window and remaining > 0:
                raw = await window.pop(0)
                if next_idx < len(offsets):
                    window.append(asyncio.create_task(_fetch_chunk(manager, message, offsets[next_idx])))
                    next_idx += 1
                if first_chunk:
                    raw = raw[leading_trim:]
                    first_chunk = False
                if not raw:
                    break
                if capture is not None and len(capture) < THUMBNAIL_CAPTURE_BYTES:
                    capture.extend(raw[: THUMBNAIL_CAPTURE_BYTES - len(capture)])
                piece = raw[:remaining] if len(raw) > remaining else raw
                remaining -= len(piece)
                yield piece
        except Exception:
            # Mid-stream failure (a transient Telegram/network error) --
            # log it and just end the response here rather than letting an
            # unhandled exception blow up the ASGI task group. The video
            # element sees a short response and can retry with a fresh
            # Range request, same as any real-world network hiccup.
            logger.exception("Streaming failed for file %s at offset %d", file_id, start)
        finally:
            for t in window:
                t.cancel()
            if window:
                await asyncio.gather(*window, return_exceptions=True)
            if capture is not None:
                # `is not None`, not truthiness -- an empty bytearray (the
                # claimed request captured zero bytes, e.g. it errored out
                # immediately) is falsy but still must release the claim,
                # or claim_thumbnail_probe stays stuck marking this file
                # in-flight forever and no future request ever retries it.
                asyncio.create_task(_finish_thumbnail_capture(manager, file_row, bytes(capture)))

    return StreamingResponse(
        body(),
        status_code=status_code,
        media_type=file_row["mime_type"] or "application/octet-stream",
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )
