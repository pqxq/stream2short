"""Shared language-code helpers for the multi-language clip studio.

The rest of the application stores languages as lower-case ISO 639-1 codes
(for example ``en``, ``uk`` or ``ja``).  This module keeps that normalisation
in one place so the fetcher, transcriber, renderer, uploader and UI agree on
how codes are validated and displayed.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_SUPPORTED_LANGUAGES: list[str] = [
    "en",
    "uk",
    "es",
    "de",
    "fr",
    "pl",
    "pt",
    "ru",
    "ja",
    "ko",
]

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "uk": "Ukrainian",
    "es": "Spanish",
    "de": "German",
    "fr": "French",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "it": "Italian",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "zh": "Chinese",
    "nl": "Dutch",
    "sv": "Swedish",
    "id": "Indonesian",
    "vi": "Vietnamese",
    "th": "Thai",
    "cs": "Czech",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "ro": "Romanian",
    "hu": "Hungarian",
    "el": "Greek",
    "he": "Hebrew",
    "ms": "Malay",
    "tl": "Filipino",
}

_LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}$")
_ALL_ALIASES = {"", "all", "any", "auto", "*", "none", "null"}


def normalize_language(
    value: Any,
    *,
    allow_none: bool = True,
    default: str | None = None,
) -> str | None:
    """Normalise a language value to a lower-case ISO 639-1 code.

    ``None``/``all``/``auto`` become ``None`` when ``allow_none`` is true, so
    callers can express "any language".  Region suffixes such as ``en-US`` and
    ``pt_BR`` are reduced to their base code.
    """
    if value is None:
        return None if allow_none else default

    text = str(value).strip().lower()
    if text in _ALL_ALIASES:
        return None if allow_none else default

    base = re.split(r"[-_]", text, maxsplit=1)[0].strip()
    if not _LANGUAGE_CODE_PATTERN.fullmatch(base):
        return None if allow_none else default
    return base


def validate_language_code(value: Any) -> str:
    """Return a validated language code or raise :class:`ValueError`."""
    code = normalize_language(value, allow_none=False)
    if not code:
        raise ValueError(
            "A valid ISO 639-1 language code (for example 'en', 'uk', 'ja') is required."
        )
    return code


def language_name(code: str | None) -> str:
    """Human-readable language name with a safe fallback to the upper-case code."""
    normalized = normalize_language(code)
    if not normalized:
        return "Auto / Any"
    return LANGUAGE_NAMES.get(normalized, normalized.upper())


def language_badge(code: str | None) -> str:
    """Compact badge used in clip cards and headings, e.g. ``[EN]``."""
    normalized = normalize_language(code)
    return f"[{normalized.upper()}]" if normalized else "[AUTO]"


def supported_languages_from_config(config: dict[str, Any] | None = None) -> list[str]:
    """Read a supported-language list from config, falling back to defaults."""
    supported: list[str] = []
    if isinstance(config, dict):
        raw = config.get("supported_languages")
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                normalized = normalize_language(item)
                if normalized and normalized not in supported:
                    supported.append(normalized)
    return supported or list(DEFAULT_SUPPORTED_LANGUAGES)


def language_options(config: dict[str, Any] | None = None) -> list[str]:
    """Return a de-duplicated supported language list including common extras."""
    codes = supported_languages_from_config(config)
    for code in DEFAULT_SUPPORTED_LANGUAGES:
        if code not in codes:
            codes.append(code)
    return codes
