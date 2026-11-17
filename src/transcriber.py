"""Speech transcription, subtitle generation and EN -> RU localisation.

The module is deliberately import-safe: expensive Whisper models are loaded
lazily, and every network/LLM failure degrades gracefully to offline
translation templates so a batch render never crashes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import openai
import yaml
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
WORKSPACE_TEMP_DIR = PROJECT_ROOT / "workspace" / "temp"
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_CONFIG: dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "whisper_model": "small",
    "compute_type": "int8",
    "font_family": "DejaVu Sans",
    "font_size": 36,
    "primary_color": "#FFFF00",
    "outline_thickness": 2,
    "margin_v": 220,
}
MAX_LINE_CHARS = 32
MAX_HOOK_CHARS = 45
WHISPER_SAMPLE_RATE = 16000


def load_config() -> dict[str, Any]:
    """Load config.yaml merged over safe defaults."""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not read %s: %s", CONFIG_PATH, exc)
    return config


def ensure_dirs() -> None:
    WORKSPACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {detail}") from exc


class WhisperTranscriber:
    """Lazy faster-whisper wrapper that only needs CPU by default."""

    def __init__(
        self,
        model_name: str = DEFAULT_CONFIG["whisper_model"],
        compute_type: str = DEFAULT_CONFIG["compute_type"],
        device: str = "cpu",
    ) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self.device = device
        self._model: WhisperModel | None = None

    @property
    def model(self) -> WhisperModel:
        if self._model is None:
            try:
                self._model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                )
            except (RuntimeError, ValueError) as exc:
                logger.warning("Whisper int8 load failed (%s); retrying with float32.", exc)
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="float32",
                )
            logger.info("Whisper model '%s' loaded on %s", self.model_name, self.device)
        return self._model

    def extract_audio(self, video_path: Path) -> Path:
        """Extract a mono 16 kHz WAV suitable for faster-whisper."""
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Media file not found: {video_path}")
        ensure_dirs()
        audio_path = WORKSPACE_TEMP_DIR / f"{video_path.stem}_audio.wav"
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(WHISPER_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            ]
        )
        return audio_path

    def transcribe(self, audio_path: Path) -> list[dict[str, Any]]:
        """Return non-empty segments as start/end/text dictionaries."""
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        segments, _info = self.model.transcribe(
            str(audio_path),
            language="en",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        result: list[dict[str, Any]] = []
        for segment in segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            result.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "text": text,
                }
            )
        logger.info("Transcribed %d segments from %s", len(result), audio_path.name)
        return result


class ASSGenerator:
    """Render segments into a mobile-safe 1080x1920 ASS subtitle file."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config()
        self.width = int(self.config["width"])
        self.height = int(self.config["height"])
        self.font_family = str(self.config["font_family"])
        self.font_size = int(self.config["font_size"])
        self.primary_color = self.hex_to_ass_bgr(str(self.config["primary_color"]))
        self.outline_thickness = int(self.config["outline_thickness"])
        self.margin_v = int(self.config["margin_v"])
        self.max_line_chars = MAX_LINE_CHARS

    @staticmethod
    def hex_to_ass_bgr(hex_color: str) -> str:
        value = hex_color.strip().lstrip("#")
        if len(value) != 6:
            value = "FFFF00"
        try:
            red = int(value[0:2], 16)
            green = int(value[2:4], 16)
            blue = int(value[4:6], 16)
        except ValueError:
            red, green, blue = 255, 255, 0
        return f"&H00{blue:02X}{green:02X}{red:02X}&"

    def wrap(self, text: str) -> list[str]:
        """Wrap at word boundaries, force-splitting words longer than the limit."""
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []
        words: list[str] = []
        for word in normalized.split(" "):
            while len(word) > self.max_line_chars:
                words.append(word[: self.max_line_chars])
                word = word[self.max_line_chars :]
            words.append(word)
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= self.max_line_chars:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        safe = max(0.0, float(seconds))
        hours = int(safe // 3600)
        minutes = int((safe % 3600) // 60)
        whole_seconds = int(safe % 60)
        centiseconds = round((safe - int(safe)) * 100)
        if centiseconds >= 100:
            centiseconds = 99
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"

    def build(self, segments: list[dict[str, Any]], lang: str = "en") -> str:
        """Build a complete ASS document including styles and events."""
        lines = [
            "[Script Info]",
            "Title: GTA6 Clip Factory",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {self.width}",
            f"PlayResY: {self.height}",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
                "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding"
            ),
            (
                f"Style: Default,{self.font_family},{self.font_size},{self.primary_color},"
                f"{self.primary_color},&H00000000&,&H00000000&,1,0,0,0,100,100,0,0,1,"
                f"{self.outline_thickness},0,2,100,100,{self.margin_v},1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for segment in segments:
            text = str(segment.get("text") or "").strip()
            wrapped = self.wrap(text)
            if not wrapped:
                continue
            start = self.format_timestamp(float(segment.get("start") or 0.0))
            end = self.format_timestamp(float(segment.get("end") or 0.0))
            dialogue_text = r"\N".join(self._escape(line) for line in wrapped)
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,{self.margin_v},,{dialogue_text}")
        lines.append("")
        return "\n".join(lines)

    def write(self, segments: list[dict[str, Any]], path: Path, lang: str = "en") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.build(segments, lang=lang), encoding="utf-8")
        return path


def _trim_hook(text: str, fallback_en: str, fallback_ru: str, lang: str) -> str:
    value = re.sub(r"\s+", " ", text).strip().strip("\"'")
    if not value:
        return fallback_en if lang == "en" else fallback_ru
    if len(value) > MAX_HOOK_CHARS:
        value = value[: MAX_HOOK_CHARS - 3].rstrip(" ,.!?-") + "..."
    return value


class TranslationHookGenerator:
    """LLM-first translation with deep-translator/template fallbacks."""

    def __init__(
        self,
        llm_url: str | None = None,
        llm_key: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.llm_url = llm_url or os.getenv("LLM_BASE_URL")
        self.llm_key = llm_key or os.getenv("LLM_API_KEY")
        self.llm_model = llm_model or os.getenv("LLM_MODEL")
        self.source = "template"
        self.client: openai.OpenAI | None = None
        if self.llm_key and self.llm_url:
            self.client = openai.OpenAI(api_key=self.llm_key, base_url=self.llm_url)
        logger.info(
            "Translation backend: %s",
            "LLM" if self.client else "deep-translator/template fallback",
        )

    def _template_hooks(self, english_text: str, russian_text: str) -> tuple[str, str]:
        english_words = english_text.split()
        russian_words = russian_text.split()
        en_phrase = " ".join(english_words[:4]) if english_words else "this GTA VI moment"
        ru_phrase = " ".join(russian_words[:4]) if russian_words else "этот момент из GTA VI"
        en_hook = _trim_hook(f"Wait for it... {en_phrase}!", "GTA VI MOMENT!", "", "en")
        ru_hook = _trim_hook(f"Ты не поверишь: {ru_phrase}!", "", "МОМЕНТ ИЗ GTA VI!", "ru")
        return en_hook, ru_hook

    @staticmethod
    def _parse_json_payload(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _llm_translate(
        self, texts: list[str], full_text: str
    ) -> tuple[list[str] | None, str | None, str | None]:
        if not self.client:
            return None, None, None
        prompt = (
            "Translate each English gaming-clip subtitle into natural, idiomatic Russian. "
            "Keep the same array length and order. Then write one punchy English hook and "
            "one punchy Russian hook, each at most 45 characters. Return strict JSON with "
            'keys "translations" (array of strings), "en_hook" and "ru_hook".\n'
            f"Subtitles: {json.dumps(texts, ensure_ascii=False)}\n"
            f"Full transcript: {full_text}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.llm_model or "gpt-3.5-turbo",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a professional gaming localisation editor.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=2048,
            )
            content = response.choices[0].message.content or ""
            payload = self._parse_json_payload(content)
            translations = payload.get("translations")
            if not isinstance(translations, list) or len(translations) != len(texts):
                return None, None, None
            ru_values = [str(item).strip() for item in translations]
            en_hook = payload.get("en_hook")
            ru_hook = payload.get("ru_hook")
            return (
                ru_values,
                str(en_hook) if en_hook else None,
                str(ru_hook) if ru_hook else None,
            )
        except (
            openai.APIError,
            openai.APIConnectionError,
            openai.RateLimitError,
            ValueError,
            KeyError,
            IndexError,
        ) as exc:
            logger.warning("LLM translation failed (%s); using offline fallback.", exc)
            return None, None, None

    def _deep_translate(self, texts: list[str]) -> list[str] | None:
        if not texts:
            return []
        try:
            translator = GoogleTranslator(source="auto", target="ru")
            translated = translator.translate_batch(texts)
            if isinstance(translated, list) and len(translated) == len(texts):
                return [str(item or "").strip() for item in translated]
        except (RuntimeError, ValueError, OSError, AttributeError) as exc:
            logger.warning("deep-translator failed (%s); keeping source text.", exc)
        return None

    def translate_and_hooks(self, text: str) -> dict[str, Any]:
        """Translate plain text and produce EN/RU hooks. Never raises."""
        english = re.sub(r"\s+", " ", text).strip()
        translations, en_hook, ru_hook = self._llm_translate([english] if english else [], english)
        if translations is not None:
            self.source = "llm"
            russian = translations[0] if translations else ""
        else:
            fallback = self._deep_translate([english] if english else [])
            if fallback is not None:
                self.source = "deeptranslator"
                russian = fallback[0] if fallback else ""
            else:
                self.source = "template"
                russian = english
        template_en, template_ru = self._template_hooks(english, russian)
        return {
            "translation": russian,
            "en_hook": _trim_hook(en_hook or "", template_en, template_ru, "en"),
            "ru_hook": _trim_hook(ru_hook or "", template_en, template_ru, "ru"),
            "source": self.source,
        }

    def translate_segments(self, segments: list[dict[str, Any]]) -> dict[str, Any]:
        """Translate all segments while preserving indexes and timings."""
        texts = [str(segment.get("text") or "").strip() for segment in segments]
        full_text = " ".join(text for text in texts if text).strip()
        if not texts:
            template_en, template_ru = self._template_hooks("", "")
            return {
                "translations": [],
                "en_hook": template_en,
                "ru_hook": template_ru,
                "source": "template",
            }
        translations, en_hook, ru_hook = self._llm_translate(texts, full_text)
        if translations is not None:
            self.source = "llm"
        else:
            translations = self._deep_translate(texts)
            if translations is not None:
                self.source = "deeptranslator"
            else:
                self.source = "template"
                translations = texts
        russian_text = " ".join(item for item in translations if item).strip()
        template_en, template_ru = self._template_hooks(full_text, russian_text)
        return {
            "translations": translations,
            "en_hook": _trim_hook(en_hook or "", template_en, template_ru, "en"),
            "ru_hook": _trim_hook(ru_hook or "", template_en, template_ru, "ru"),
            "source": self.source,
        }


class SubtitleProcessor:
    """End-to-end speech -> EN/RU ASS and hooks."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config()
        self.ass_generator = ASSGenerator(self.config)
        self.translator = TranslationHookGenerator()
        self._whisper: WhisperTranscriber | None = None

    @property
    def whisper(self) -> WhisperTranscriber:
        if self._whisper is None:
            self._whisper = WhisperTranscriber(
                model_name=str(self.config.get("whisper_model", "small")),
                compute_type=str(self.config.get("compute_type", "int8")),
            )
        return self._whisper

    def process(self, media_path: Path, clip_id: str) -> dict[str, Any]:
        ensure_dirs()
        media_path = Path(media_path)
        if not media_path.exists():
            raise FileNotFoundError(f"Media file not found: {media_path}")
        clip_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", clip_id).strip("._") or media_path.stem

        audio_path = self.whisper.extract_audio(media_path)
        segments = self.whisper.transcribe(audio_path)

        translation = self.translator.translate_segments(segments)
        ru_segments = [
            {**segment, "text": translated}
            for segment, translated in zip(segments, translation["translations"], strict=False)
        ]

        ass_en = WORKSPACE_TEMP_DIR / f"{clip_id}_en.ass"
        ass_ru = WORKSPACE_TEMP_DIR / f"{clip_id}_ru.ass"
        self.ass_generator.write(segments, ass_en, lang="en")
        self.ass_generator.write(ru_segments, ass_ru, lang="ru")

        payload = {
            "clip_id": clip_id,
            "media_path": str(media_path),
            "audio_path": str(audio_path),
            "segments_en": segments,
            "segments_ru": ru_segments,
            "ass_path_en": str(ass_en),
            "ass_path_ru": str(ass_ru),
            "translation": translation.get("translation", ""),
            "en_hook": translation["en_hook"],
            "ru_hook": translation["ru_hook"],
            "hook_source": translation["source"],
        }
        translation_file = WORKSPACE_TEMP_DIR / f"{clip_id}_translation.json"
        translation_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Subtitle processing complete for %s (%d segments)", clip_id, len(segments))
        return payload


def process_media(media_path: Path, clip_id: str) -> dict[str, Any]:
    """Convenience wrapper used by the UI and smoke tests."""
    return SubtitleProcessor().process(media_path, clip_id)
