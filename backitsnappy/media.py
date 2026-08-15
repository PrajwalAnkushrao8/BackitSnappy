"""Thumbnail generation and the local media cache, keyed by sha256 hash.

Thumbnails are generated once, synchronously, at upload time from the local
source file already on disk — never round-tripped through Telegram. Images
use Pillow (with EXIF-orientation correction, since iPhone photos are stored
in sensor orientation plus a rotation flag, not pre-rotated pixels); videos
use an ffmpeg subprocess to grab one frame. Both failure modes (corrupt
file, missing ffmpeg) degrade to "no thumbnail" rather than raising, so a
bad file never blocks an upload.
"""
import logging
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from . import config

logger = logging.getLogger(__name__)

# Fraction of the cap to evict back down to on every sweep, rather than
# stopping the instant we're back under the limit -- avoids re-triggering
# an eviction pass on almost every subsequent cache write.
CACHE_EVICTION_TARGET_RATIO = 0.9

THUMBNAIL_SIZE = 320
THUMBNAIL_QUALITY = 85
FFMPEG_TIMEOUT_SECONDS = 30


def _resolve_ffmpeg() -> str | None:
    """Prefer a system ffmpeg on PATH (respects an existing install), and
    fall back to the binary bundled by the imageio-ffmpeg package so users
    never need to `brew install ffmpeg` themselves. None if truly
    unavailable (e.g. the pip package failed to install on an unsupported
    platform) -- callers already degrade to "no thumbnail" in that case."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.exception("Bundled ffmpeg (imageio-ffmpeg) unavailable")
        return None

def _thumbnails_dir() -> Path:
    # A function, not a module-level constant, so it picks up config.APP_SUPPORT_DIR
    # being overridden (e.g. in tests) even after this module has been imported.
    return config.APP_SUPPORT_DIR / "thumbnails"


def _media_cache_dir() -> Path:
    return config.APP_SUPPORT_DIR / "media_cache"


def classify(mime_type: str | None) -> str:
    if not mime_type:
        return "other"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return "other"


def safe_filename(name: str) -> str:
    """Reduces an untrusted filename to a single, inert path component.

    Filenames reaching this app are not trustworthy: a synced file's name
    comes straight from `msg.file.name` on the Telegram message (see
    client_manager._import_message), which is chosen by whoever uploaded
    it -- and anyone invited to a shared album can upload. An uploaded
    file's name likewise comes from the multipart request. Joining such a
    name onto a directory with `Path / name` honors embedded separators
    and `..` segments, so it escapes that directory entirely (verified:
    a crafted name resolved from ~/Downloads/BackitSnappy all the way to
    /Library/LaunchAgents/, which is a persistence primitive).

    Anything that builds a real path from one of these names must route
    it through here first. Note media.cached_media_path is already immune
    by construction -- it keeps only `Path(name).suffix`, never the name.
    """
    name = os.path.basename(name or "").replace("\x00", "")
    name = name.replace("/", "_").replace("\\", "_").strip()
    if name in ("", ".", ".."):
        name = "unnamed"
    # Leave room for callers that add their own prefix/suffix (a uuid
    # prefix, a ".part" suffix) inside the usual 255-byte filename limit.
    return name[:200]


def thumbnail_path(sha256_hash: str) -> Path:
    return _thumbnails_dir() / f"{sha256_hash}.jpg"


def cached_media_path(sha256_hash: str, filename: str) -> Path:
    suffix = Path(filename).suffix
    return _media_cache_dir() / f"{sha256_hash}{suffix}"


def touch_cache_access(path: Path) -> None:
    """Bumps a cached file's mtime to "now" -- the cheap last-used signal
    enforce_cache_limit() evicts by. Call this on every cache *hit* (the
    file already existed, so nothing else naturally updates its mtime) so
    frequently-viewed files are the least likely to be evicted."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def prune_orphaned_cache(known_hashes: set[str]) -> int:
    """Deletes any media_cache/thumbnail file whose hash isn't in
    known_hashes -- e.g. after db.wipe_local_index() clears the database
    but leaves the actual cached bytes on disk completely untouched (that
    function is pure SQL by design; this is the filesystem half the caller
    must also run, using db.get_all_known_hashes() for known_hashes).
    Returns the number of files removed."""
    removed = 0
    for directory in (_media_cache_dir(), _thumbnails_dir()):
        if not directory.exists():
            continue
        for f in directory.iterdir():
            if not f.is_file():
                continue
            if f.stem not in known_hashes:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    continue
    return removed


def enforce_cache_limit() -> None:
    """Keeps media_cache (full-resolution cached originals) under
    config's media_cache_max_bytes by deleting the least-recently-used
    files first. Thumbnails are untouched -- they're tiny, and needed for
    browsing without a full re-download. Safe to evict anything here:
    Telegram is always the source of truth, and ensure_local_media already
    re-downloads on a cache miss. Pure local filesystem work (no network,
    no DB), so it's cheap enough to call synchronously from a thread after
    every cache write."""
    cache_dir = _media_cache_dir()
    if not cache_dir.exists():
        return
    max_bytes = config.get("media_cache_max_bytes")
    entries = []
    total = 0
    for f in cache_dir.iterdir():
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        entries.append((f, st.st_size, st.st_mtime))
        total += st.st_size
    if total <= max_bytes:
        return

    target = max_bytes * CACHE_EVICTION_TARGET_RATIO
    entries.sort(key=lambda e: e[2])  # oldest-accessed first
    # Never evict the single most-recently-touched entry, even if it alone
    # exceeds the cap (e.g. one large video with a small configured limit)
    # -- this always runs right after that entry was written, so deleting
    # it here would hand the caller a path to a file that's already gone.
    # Staying slightly over the cap in that rare case beats a dangling path.
    protected = entries[-1][0] if entries else None
    for f, size, _mtime in entries:
        if f == protected:
            continue
        if total <= target:
            break
        try:
            f.unlink()
            total -= size
        except OSError:
            continue
    logger.info("media_cache evicted down to %d bytes (cap %d)", total, max_bytes)


def generate_thumbnail(
    source_path: Path, sha256_hash: str, mime_type: str | None, force_seek_offset: float | None = None,
) -> bool:
    """Generate a thumbnail for source_path if one doesn't already exist.
    Returns whether a thumbnail is now available. Never raises.

    force_seek_offset overrides _pick_seek_offset's normal "a bit into the
    clip, not just frame 0" heuristic -- needed for a reconstructed
    head+tail file (see generate_video_thumbnail_from_head_tail), whose
    moov atom reports the *real* full-length duration even though only
    the first few seconds of actual sample data are physically present;
    without forcing 0.0 there, the auto-picked offset routinely lands
    past the available data and produces no frame at all."""
    dest = thumbnail_path(sha256_hash)
    if dest.exists():
        return True

    kind = classify(mime_type)
    try:
        _thumbnails_dir().mkdir(parents=True, exist_ok=True)
        if kind == "image":
            return _generate_image_thumbnail(source_path, dest)
        if kind == "video":
            return _generate_video_thumbnail(source_path, dest, force_seek_offset)
        return False
    except Exception:
        logger.exception("Thumbnail generation failed for %s", source_path)
        dest.unlink(missing_ok=True)
        return False


def _generate_image_thumbnail(source_path: Path, dest: Path) -> bool:
    with Image.open(source_path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        image.save(dest, "JPEG", quality=THUMBNAIL_QUALITY)
    return True


def _generate_video_thumbnail(source_path: Path, dest: Path, force_seek_offset: float | None = None) -> bool:
    ffmpeg = _resolve_ffmpeg()
    if ffmpeg is None:
        logger.warning("No ffmpeg available (system or bundled); skipping video thumbnail for %s", source_path)
        return False

    offset = force_seek_offset if force_seek_offset is not None else _pick_seek_offset(source_path)
    result = subprocess.run(
        [
            ffmpeg, "-y",
            "-ss", f"{offset:.3f}",
            "-i", str(source_path),
            "-frames:v", "1",
            "-q:v", "2",
            "-vf", f"scale={THUMBNAIL_SIZE}:-1",
            str(dest),
        ],
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        logger.warning(
            "ffmpeg thumbnail extraction failed for %s: %s",
            source_path, result.stderr.decode(errors="replace")[:500],
        )
        return False
    return True


def _patch_truncated_head(head: bytes) -> bytes:
    """If the last top-level MP4/MOV box in `head` was cut off mid-box --
    expected, since `head` is only the first slice of a much larger file
    -- rewrites that box's declared size field to match what's actually
    present, leaving every earlier, fully-captured box untouched. Needed
    because ffmpeg's mov demuxer walks top-level boxes by their declared
    size to find the next one; with the original (much larger) size still
    in place, it tries to skip past bytes that were never fetched and
    reports "moov atom not found" even once a real moov box has been
    appended right where that skip should have landed -- confirmed
    directly: identical reconstructed bytes failed before this patch and
    produced a real thumbnail after it. Byte-for-byte identical to `head`
    if the last box already happens to be complete."""
    pos, n = 0, len(head)
    truncated_at = None
    while pos + 8 <= n:
        size = struct.unpack(">I", head[pos:pos + 4])[0]
        real_size = size
        if size == 1:
            if pos + 16 > n:
                truncated_at = pos
                break
            real_size = struct.unpack(">Q", head[pos + 8:pos + 16])[0]
        if real_size == 0 or pos + real_size > n:
            truncated_at = pos
            break
        pos += real_size
    if truncated_at is None:
        return head
    patched = bytearray(head)
    truncated_len = n - truncated_at
    size_field = struct.unpack(">I", head[truncated_at:truncated_at + 4])[0]
    if size_field == 1:
        patched[truncated_at + 8:truncated_at + 16] = struct.pack(">Q", truncated_len)
    else:
        patched[truncated_at:truncated_at + 4] = struct.pack(">I", truncated_len)
    return bytes(patched)


def _find_moov_atom(tail: bytes, tail_abs_start: int, file_size: int) -> tuple[int, int] | None:
    """Searches `tail` (however much of the file's end was fetched) for a
    moov box, returning its (relative_start, relative_end) within `tail`
    if found. moov is almost always the very last top-level box in
    unprocessed/non-faststart video (camera-original iPhone footage in
    particular), so among every literal "moov" byte match in the buffer
    (sample data can coincidentally contain those 4 bytes -- it's raw
    compressed video/audio), the real box is picked by whichever
    candidate's declared end lands closest to the file's actual end."""
    candidates = []
    search_end = len(tail)
    while True:
        idx = tail.rfind(b"moov", 0, search_end)
        if idx == -1 or idx < 4:
            break
        size = struct.unpack(">I", tail[idx - 4:idx])[0]
        box_start_abs = tail_abs_start + idx - 4
        box_end_abs = box_start_abs + size
        if size >= 8 and box_end_abs <= file_size and idx - 4 + size <= len(tail):
            candidates.append((idx - 4, idx - 4 + size, box_end_abs))
        search_end = idx
    if not candidates:
        return None
    candidates.sort(key=lambda c: abs(c[2] - file_size))
    rel_start, rel_end, _ = candidates[0]
    return rel_start, rel_end


def generate_video_thumbnail_from_head_tail(
    head: bytes, tail: bytes, tail_abs_start: int, file_size: int,
    sha256_hash: str, mime_type: str | None,
) -> bool:
    """Generates a thumbnail for a video without ever downloading it in
    full -- for camera-original footage whose moov (index) atom sits at
    the very end of the file rather than the start, a head-only capture
    (see generate_thumbnail) can never work no matter how much of the
    head it grabs, since the index simply isn't there. This locates moov
    within `tail` (see _find_moov_atom) and splices it onto `head` (see
    _patch_truncated_head for why a raw concatenation alone isn't enough),
    producing a short file ffmpeg can actually read the first frame from
    -- even though it's nowhere near byte-for-byte identical to the real
    file. Returns False, without raising, if moov isn't present in the
    given `tail` window at all (the caller can retry with a larger tail)."""
    found = _find_moov_atom(tail, tail_abs_start, file_size)
    if found is None:
        return False
    rel_start, rel_end = found
    reconstructed = _patch_truncated_head(head) + tail[rel_start:rel_end]

    tmp_dir = Path(tempfile.mkdtemp(prefix="backitsnappy-thumb-recon-"))
    try:
        tmp_path = tmp_dir / "probe.mp4"
        tmp_path.write_bytes(reconstructed)
        return generate_thumbnail(tmp_path, sha256_hash, mime_type, force_seek_offset=0.0)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pick_seek_offset(source_path: Path) -> float:
    """Pick a safe seek offset for the thumbnail frame, clamped to the
    video's actual duration so short clips don't produce an empty frame.
    Only checks system PATH -- imageio-ffmpeg (the pip-bundled fallback
    above) doesn't ship ffprobe, only ffmpeg. That's fine: this is a purely
    cosmetic best-effort (a better mid-clip frame instead of the very
    first one), already designed to degrade to 0.0 when unavailable."""
    if shutil.which("ffprobe") is None:
        return 0.0
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(source_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        return 0.0
    return min(1.0, duration * 0.1) if duration > 0 else 0.0
