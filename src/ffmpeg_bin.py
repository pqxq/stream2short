"""Cross-platform FFmpeg/ffprobe binary resolution helpers.

``ffmpeg_exe`` prefers an FFmpeg on PATH and falls back to the bundled
``imageio-ffmpeg`` binary so pip-only installs still render.  ``ffprobe_exe``
resolves the system ffprobe only; ``imageio-ffmpeg`` does not ship one.
"""

from __future__ import annotations

import logging
import shutil

logger = logging.getLogger(__name__)


def ffmpeg_exe() -> str | None:
    """Return an absolute ffmpeg binary path, or None when unavailable."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - ImportError or runtime failure = no ffmpeg
        return None


def ffprobe_exe() -> str | None:
    """Return an absolute ffprobe binary path from the system, or None."""
    return shutil.which("ffprobe")


def has_ffmpeg() -> bool:
    return ffmpeg_exe() is not None


def has_ffprobe() -> bool:
    return ffprobe_exe() is not None
