"""Cross-platform font resolution for FFmpeg filters and Pillow previews.

Candidates cover Linux (DejaVu/Liberation/Noto), macOS (Arial/Helvetica system
fonts) and Windows (Arial/Segoe UI via ``WINDIR``).  ``resolve_font_path``
never returns a non-existent path; ``font_family_for`` maps per-platform
family names, including CJK fallbacks.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

_IS_WINDOWS = platform.system() == "Windows"
_IS_MACOS = platform.system() == "Darwin"


def _windows_dir() -> Path:
    return Path(
        os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    )


if _IS_WINDOWS:
    _win_fonts = _windows_dir() / "Fonts"
    FONT_CANDIDATES = (
        str(_win_fonts / "arialbd.ttf"),
        str(_win_fonts / "segoeuib.ttf"),
        str(_win_fonts / "arial.ttf"),
        str(_win_fonts / "segoeui.ttf"),
    )
    CJK_FONT_CANDIDATES = (
        str(_win_fonts / "msgothic.ttc"),
        str(_win_fonts / "yugothm.ttc"),
        str(_win_fonts / "malgun.ttf"),
    )
    CJK_FONT_FAMILIES = {"ja": "MS Gothic", "ko": "Malgun Gothic"}
elif _IS_MACOS:
    FONT_CANDIDATES = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Helvetica.ttf",
    )
    CJK_FONT_CANDIDATES = (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Apple Gothic.ttf",
    )
    CJK_FONT_FAMILIES = {"ja": "Hiragino Sans", "ko": "Apple SD Gothic Neo"}
else:
    FONT_CANDIDATES = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    CJK_FONT_CANDIDATES = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    )
    CJK_FONT_FAMILIES = {"ja": "Noto Sans CJK JP", "ko": "Noto Sans CJK KR"}


def resolve_font_path(lang_code: str | None = None) -> str | None:
    """Return the first existing font file for the language, or None."""
    code = (lang_code or "").strip().lower()
    if code in CJK_FONT_FAMILIES:
        for candidate in CJK_FONT_CANDIDATES:
            if Path(candidate).exists():
                return candidate
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def font_family_for(lang_code: str | None = None) -> str:
    """Return a font family name matching the platform for the language."""
    code = (lang_code or "").strip().lower()
    if code in CJK_FONT_FAMILIES and resolve_font_path(code) is not None:
        return CJK_FONT_FAMILIES[code]
    if _IS_WINDOWS:
        return "Arial"
    if _IS_MACOS:
        return "Helvetica"
    return "DejaVu Sans"
