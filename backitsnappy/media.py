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
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from . import config

logger = logging.getLogger(__name__)

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


def thumbnail_path(sha256_hash: str) -> Path:
    return _thumbnails_dir() / f"{sha256_hash}.jpg"


def cached_media_path(sha256_hash: str, filename: str) -> Path:
    suffix = Path(filename).suffix
    return _media_cache_dir() / f"{sha256_hash}{suffix}"


def generate_thumbnail(source_path: Path, sha256_hash: str, mime_type: str | None) -> bool:
    """Generate a thumbnail for source_path if one doesn't already exist.
    Returns whether a thumbnail is now available. Never raises."""
    dest = thumbnail_path(sha256_hash)
    if dest.exists():
        return True

    kind = classify(mime_type)
    try:
        _thumbnails_dir().mkdir(parents=True, exist_ok=True)
        if kind == "image":
            return _generate_image_thumbnail(source_path, dest)
        if kind == "video":
            return _generate_video_thumbnail(source_path, dest)
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


def _generate_video_thumbnail(source_path: Path, dest: Path) -> bool:
    ffmpeg = _resolve_ffmpeg()
    if ffmpeg is None:
        logger.warning("No ffmpeg available (system or bundled); skipping video thumbnail for %s", source_path)
        return False

    offset = _pick_seek_offset(source_path)
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
