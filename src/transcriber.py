"""Speech transcription, subtitle generation and multi-language localisation.

Native mode transcribes directly in the clip language and generates subtitles,
hook banner text, title, description/hashtags and a pinned-comment CTA in that
same language.  Optional translate mode can add a second language variant via
an OpenAI-compatible LLM, with a Deep-Translator/template fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openai
import yaml
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel

from src.languages import (
    DEFAULT_SUPPORTED_LANGUAGES,
    language_name,
    normalize_language,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
WORKSPACE_TEMP_DIR = PROJECT_ROOT / "workspace" / "temp"
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_CONFIG: dict[str, Any] = {
    "default_game": "Grand Theft Auto VI",
    "default_language": "en",
    "supported_languages": list(DEFAULT_SUPPORTED_LANGUAGES),
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
MAX_TITLE_CHARS = 100
WHISPER_SAMPLE_RATE = 16000
CJK_FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)

NATIVE_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "hook_prefix": "Wait for it...",
        "hook_suffix": "!",
        "hook_fallback": "You will not believe this moment!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nAn insane {game} moment in vertical format. "
            "Subscribe for daily Shorts and viral clips!"
        ),
        "cta": (
            "Which moment was your favourite? Tell us below.\n"
            "Subscribe so you never miss the next one!"
        ),
        "hashtags": "#Gaming #Shorts #Viral #Clips",
    },
    "uk": {
        "hook_prefix": "Зачекай...",
        "hook_suffix": "!",
        "hook_fallback": "Ти не повіриш, що сталося!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nНеймовірний момент із {game} у вертикальному форматі. "
            "Підписуйся, щоб не пропустити нові Shorts!"
        ),
        "cta": (
            "Який момент сподобався найбільше? Напиши в коментарях!\n"
            "Підписуйся, щоб не пропустити наступний кліп."
        ),
        "hashtags": "#Ігри #Shorts #Українською #Gaming",
    },
    "es": {
        "hook_prefix": "Espera...",
        "hook_suffix": "!",
        "hook_fallback": "¡No te vas a creer este momento!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nUn momento increíble de {game} en formato vertical. "
            "¡Suscríbete para más Shorts diarios!"
        ),
        "cta": (
            "¿Cuál fue tu momento favorito? Cuéntanoslo abajo.\n"
            "¡Suscríbete para no perderte el siguiente clip!"
        ),
        "hashtags": "#Gaming #Shorts #Viral #Videojuegos",
    },
    "de": {
        "hook_prefix": "Warte darauf...",
        "hook_suffix": "!",
        "hook_fallback": "Du wirst diesen Moment nicht glauben!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nEin verrückter {game}-Moment im vertikalen Format. "
            "Abonniere für tägliche Shorts!"
        ),
        "cta": (
            "Welcher Moment war dein Favorit? Schreib es in die Kommentare!\n"
            "Abonniere, damit du den nächsten Clip nicht verpasst."
        ),
        "hashtags": "#Gaming #Shorts #Viral #Spiele",
    },
    "fr": {
        "hook_prefix": "Attends...",
        "hook_suffix": " !",
        "hook_fallback": "Tu ne vas pas croire ce moment !",
        "title": "{game} : {hook} #Shorts",
        "description": (
            "{hook}\n\nUn moment incroyable de {game} au format vertical. "
            "Abonne-toi pour des Shorts quotidiens !"
        ),
        "cta": (
            "Quel moment as-tu préféré ? Dis-le en commentaire !\n"
            "Abonne-toi pour ne pas rater le prochain clip."
        ),
        "hashtags": "#Gaming #Shorts #Viral #JeuxVideo",
    },
    "pl": {
        "hook_prefix": "Czekaj...",
        "hook_suffix": "!",
        "hook_fallback": "Nie uwierzysz, co się stało!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nSzalony moment z {game} w formacie pionowym. "
            "Subskrybuj, aby nie przegapić kolejnych Shorts!"
        ),
        "cta": (
            "Który moment podobał Ci się najbardziej? Napisz w komentarzu!\n"
            "Subskrybuj, aby nie przegapić następnego klipu."
        ),
        "hashtags": "#Gry #Shorts #Viral #Gaming",
    },
    "pt": {
        "hook_prefix": "Espera...",
        "hook_suffix": "!",
        "hook_fallback": "Você não vai acreditar neste momento!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nUm momento insano de {game} em formato vertical. "
            "Inscreva-se para mais Shorts diários!"
        ),
        "cta": (
            "Qual foi o seu momento favorito? Conta pra gente abaixo!\n"
            "Inscreva-se para não perder o próximo clipe."
        ),
        "hashtags": "#Gaming #Shorts #Viral #Jogos",
    },
    "ru": {
        "hook_prefix": "Только посмотри...",
        "hook_suffix": "!",
        "hook_fallback": "Ты не поверишь, что произошло!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\nБезумный момент из {game} в вертикальном формате. "
            "Подпишись, чтобы не пропустить новые Shorts!"
        ),
        "cta": (
            "Какой момент понравился больше всего? Напиши в комментариях!\n"
            "Подпишись, чтобы не пропустить следующий клип."
        ),
        "hashtags": "#Игры #Shorts #Viral #Gaming",
    },
    "ja": {
        "hook_prefix": "待って…",
        "hook_suffix": "！",
        "hook_fallback": "この瞬間、信じられない！",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\n{game}の衝撃的な瞬間を縦型で。"
            "毎日ショート動画を投稿中！"
        ),
        "cta": (
            "一番好きな場面はどこ？コメントで教えてね！\n"
            "チャンネル登録して次のクリップもチェック！"
        ),
        "hashtags": "#ゲーム #Shorts #バイラル #Gaming",
    },
    "ko": {
        "hook_prefix": "잠깐만…",
        "hook_suffix": "!",
        "hook_fallback": "이 장면, 믿을 수 없어!",
        "title": "{game}: {hook} #Shorts",
        "description": (
            "{hook}\n\n{game}의 미친 순간을 세로형으로. "
            "매일 Shorts를 업로드합니다!"
        ),
        "cta": (
            "가장 좋아하는 장면은? 댓글로 알려줘!\n"
            "구독하고 다음 클립도 놓치지 마세요."
        ),
        "hashtags": "#게임 #Shorts #바이럴 #Gaming",
    },
}

_GENERIC_PROFILE = {
    "hook_prefix": "Wait for it...",
    "hook_suffix": "!",
    "hook_fallback": "You will not believe this moment!",
    "title": "{game}: {hook} #Shorts",
    "description": "{hook}\n\nA viral {game} moment in vertical format.",
    "cta": "Tell us your favourite moment below!\nSubscribe for more clips.",
    "hashtags": "#Gaming #Shorts #Viral",
}


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


def _trim_hook(text: str, fallback: str = "") -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'")
    if not value:
        value = fallback.strip()
    if len(value) > MAX_HOOK_CHARS:
        value = value[: MAX_HOOK_CHARS - 3].rstrip(" ,.!?-") + "..."
    return value


def _trim_title(text: str, fallback: str = "") -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        value = fallback.strip()
    if len(value) > MAX_TITLE_CHARS:
        value = value[:MAX_TITLE_CHARS].rstrip()
    return value


def _transcript_phrase(text: str, max_words: int = 6, max_chars: int = 30) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    phrase = " ".join(normalized.split()[:max_words])
    if len(phrase) > max_chars:
        phrase = phrase[:max_chars].rstrip(" ,.!?-…")
    return phrase


def _profile_for(lang: str | None) -> dict[str, str]:
    code = normalize_language(lang) or "en"
    return NATIVE_TEMPLATES.get(code, _GENERIC_PROFILE)


def build_template_metadata(
    lang: str | None,
    game_name: str = "",
    transcript: str = "",
) -> dict[str, str]:
    """Build native-language hook/title/description/CTA metadata offline."""
    code = normalize_language(lang) or "en"
    profile = _profile_for(code)
    game = str(game_name or "").strip() or "this game"
    phrase = _transcript_phrase(transcript)
    if phrase:
        hook = f"{profile['hook_prefix']} {phrase}{profile['hook_suffix']}"
    else:
        hook = profile["hook_fallback"].format(game=game)
    hook = _trim_hook(hook, profile["hook_fallback"].format(game=game))
    title = _trim_title(profile["title"].format(game=game, hook=hook))
    description = profile["description"].format(game=game, hook=hook).strip()
    hashtags = profile["hashtags"].strip()
    if hashtags and hashtags not in description:
        description = f"{description}\n\n{hashtags}"
    return {
        "hook": hook,
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "cta": profile["cta"].format(game=game).strip(),
    }


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

    def transcribe(
        self, audio_path: Path, language: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return non-empty segments plus the detected language code."""
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        source_code = normalize_language(language)
        segments, info = self.model.transcribe(
            str(audio_path),
            language=source_code,
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
        detected = normalize_language(getattr(info, "language", None))
        logger.info(
            "Transcribed %d segments from %s (requested=%s, detected=%s)",
            len(result),
            audio_path.name,
            source_code or "auto",
            detected or "unknown",
        )
        return result, detected


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

    def _font_family_for(self, lang: str | None) -> str:
        code = normalize_language(lang)
        if code in {"ja", "ko"} and any(Path(path).exists() for path in CJK_FONT_PATHS):
            return "Noto Sans CJK JP" if code == "ja" else "Noto Sans CJK KR"
        return self.font_family

    def build(self, segments: list[dict[str, Any]], lang: str = "en") -> str:
        """Build a complete ASS document including styles and events."""
        font_family = self._font_family_for(lang)
        lines = [
            "[Script Info]",
            "Title: Universal Clip Studio",
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
                f"Style: Default,{font_family},{self.font_size},{self.primary_color},"
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


class LocalizationEngine:
    """LLM-first native metadata/localisation with offline fallbacks."""

    def __init__(
        self,
        llm_url: str | None = None,
        llm_key: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.llm_url = llm_url or os.getenv("LLM_BASE_URL")
        self.llm_key = llm_key or os.getenv("LLM_API_KEY")
        self.llm_model = llm_model or os.getenv("LLM_MODEL")
        self.client: openai.OpenAI | None = None
        if self.llm_key and self.llm_url:
            self.client = openai.OpenAI(api_key=self.llm_key, base_url=self.llm_url)
        logger.info(
            "Localization backend: %s",
            "LLM" if self.client else "Deep-Translator/template fallback",
        )

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

    def _chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.client:
            return {}
        try:
            response = self.client.chat.completions.create(
                model=self.llm_model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=2048,
            )
            content = response.choices[0].message.content or ""
            return self._parse_json_payload(content)
        except Exception as exc:  # noqa: BLE001 - LLM failures must fall back
            logger.warning("LLM request failed (%s); using offline fallback.", exc)
            return {}

    @staticmethod
    def _as_text(value: Any, fallback: str = "") -> str:
        if isinstance(value, (list, tuple)):
            text = " ".join(str(item) for item in value if str(item).strip())
        else:
            text = str(value or "")
        text = re.sub(r"\s+", " ", text).strip()
        return text or fallback

    def _merge_metadata(
        self, payload: dict[str, Any], fallback: dict[str, str]
    ) -> dict[str, str]:
        metadata = dict(fallback)
        if payload:
            metadata["hook"] = _trim_hook(
                self._as_text(payload.get("hook"), fallback["hook"]), fallback["hook"]
            )
            metadata["title"] = _trim_title(
                self._as_text(payload.get("title"), fallback["title"]), fallback["title"]
            )
            metadata["description"] = self._as_text(
                payload.get("description"), fallback["description"]
            )
            metadata["hashtags"] = self._as_text(
                payload.get("hashtags"), fallback["hashtags"]
            )
            metadata["cta"] = self._as_text(payload.get("cta"), fallback["cta"])
            if metadata["hashtags"] and metadata["hashtags"] not in metadata["description"]:
                metadata["description"] = (
                    f"{metadata['description']}\n\n{metadata['hashtags']}"
                )
        return metadata

    def native_metadata(
        self,
        transcript: str,
        lang: str,
        game_name: str,
    ) -> tuple[dict[str, str], str]:
        """Generate native metadata, returning ``(metadata, source)``."""
        fallback = build_template_metadata(lang, game_name, transcript)
        if not self.client or not str(transcript).strip():
            return fallback, "template"
        language = language_name(lang)
        system = (
            "You are a professional gaming social-media editor. "
            "Always answer with strict JSON and no markdown."
        )
        prompt = (
            f"Write viral publishing metadata in {language} for a {game_name} gaming Short. "
            'Return strict JSON with keys "hook", "title", "description", "hashtags" and "cta". '
            "hook <= 45 characters, title <= 100 characters, description 2-4 sentences, "
            "cta a short pinned-comment call to action. Do not translate into another language.\n"
            f"Transcript: {transcript}"
        )
        payload = self._chat_json(system, prompt)
        if not payload:
            return fallback, "template"
        return self._merge_metadata(payload, fallback), "llm"

    def _deep_translate_texts(
        self,
        texts: list[str],
        source_lang: str | None,
        target_lang: str,
    ) -> list[str] | None:
        if not texts:
            return []
        source = normalize_language(source_lang) or "auto"
        try:
            translator = GoogleTranslator(source=source, target=target_lang)
            batch = translator.translate_batch(texts)
        except Exception as exc:  # noqa: BLE001 - offline fallback must never crash
            logger.warning("Deep-Translator batch failed (%s).", exc)
            batch = None
        translated: list[str] = []
        if isinstance(batch, list) and len(batch) == len(texts):
            for original, item in zip(texts, batch, strict=False):
                value = str(item or "").strip()
                translated.append(value or original)
            return translated
        try:
            translator = GoogleTranslator(source=source, target=target_lang)
            for text in texts:
                try:
                    value = str(translator.translate(text) or "").strip()
                except Exception:  # noqa: BLE001 - per-item fallback
                    value = ""
                translated.append(value or text)
            return translated
        except Exception as exc:  # noqa: BLE001 - offline fallback must never crash
            logger.warning("Deep-Translator individual fallback failed (%s).", exc)
            return None

    def _deep_translate_metadata(
        self,
        metadata: dict[str, str],
        source_lang: str | None,
        target_lang: str,
    ) -> dict[str, str] | None:
        fields = ["hook", "title", "description", "cta"]
        texts = [str(metadata.get(field) or "") for field in fields]
        translated = self._deep_translate_texts(texts, source_lang, target_lang)
        if translated is None:
            return None
        fallback = build_template_metadata(target_lang, "", "")
        return {
            "hook": _trim_hook(translated[0], fallback["hook"]),
            "title": _trim_title(translated[1], fallback["title"]),
            "description": translated[2] or fallback["description"],
            "hashtags": fallback["hashtags"],
            "cta": translated[3] or fallback["cta"],
        }

    def translate_segments(
        self,
        segments: list[dict[str, Any]],
        source_lang: str | None,
        target_lang: str,
        game_name: str,
        source_metadata: dict[str, str],
    ) -> dict[str, Any]:
        """Translate subtitles and metadata into ``target_lang``."""
        texts = [str(segment.get("text") or "").strip() for segment in segments]
        full_text = " ".join(text for text in texts if text)
        fallback_metadata = build_template_metadata(target_lang, game_name, full_text)
        if not texts:
            return {
                "translations": [],
                "metadata": fallback_metadata,
                "source": "template",
                "translated": False,
            }

        if self.client:
            language = language_name(target_lang)
            source_name = language_name(source_lang)
            system = (
                "You are a professional gaming localisation editor. "
                "Always answer with strict JSON and no markdown."
            )
            prompt = (
                f"Translate the following {source_name} gaming-clip subtitles into natural "
                f"idiomatic {language}. Keep the same array length and order. Also translate "
                "the publishing metadata into the same target language. Return strict JSON "
                'with keys "translations" (array of strings, same length as input), "hook", '
                '"title", "description", "hashtags" and "cta". '
                "hook <= 45 characters and title <= 100 characters.\n"
                f"Subtitles: {json.dumps(texts, ensure_ascii=False)}\n"
                f"Source hook: {source_metadata.get('hook', '')}\n"
                f"Source title: {source_metadata.get('title', '')}\n"
                f"Source description: {source_metadata.get('description', '')}\n"
                f"Source CTA: {source_metadata.get('cta', '')}"
            )
            payload = self._chat_json(system, prompt)
            translations = payload.get("translations") if payload else None
            if isinstance(translations, list) and len(translations) == len(texts):
                values = [str(item or "").strip() or texts[index] for index, item in enumerate(translations)]
                metadata = self._merge_metadata(payload, fallback_metadata)
                return {
                    "translations": values,
                    "metadata": metadata,
                    "source": "llm",
                    "translated": True,
                }

        translated_texts = self._deep_translate_texts(texts, source_lang, target_lang)
        if translated_texts is not None:
            metadata = self._deep_translate_metadata(
                source_metadata, source_lang, target_lang
            ) or fallback_metadata
            return {
                "translations": translated_texts,
                "metadata": metadata,
                "source": "deeptranslator",
                "translated": True,
            }

        return {
            "translations": texts,
            "metadata": fallback_metadata,
            "source": "template",
            "translated": False,
        }


class SubtitleProcessor:
    """End-to-end speech -> native ASS/metadata plus optional translation."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config()
        self.ass_generator = ASSGenerator(self.config)
        self.localizer = LocalizationEngine()
        self._whisper: WhisperTranscriber | None = None
        self.default_language = normalize_language(self.config.get("default_language")) or "en"
        self.default_game = str(self.config.get("default_game") or "this game").strip()

    @property
    def whisper(self) -> WhisperTranscriber:
        if self._whisper is None:
            self._whisper = WhisperTranscriber(
                model_name=str(self.config.get("whisper_model", "small")),
                compute_type=str(self.config.get("compute_type", "int8")),
            )
        return self._whisper

    def process(
        self,
        media_path: Path,
        clip_id: str,
        source_lang: str | None = "en",
        target_lang: str | None = None,
        game_name: str = "",
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        ensure_dirs()
        media_path = Path(media_path)
        if not media_path.exists():
            raise FileNotFoundError(f"Media file not found: {media_path}")
        clip_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", clip_id).strip("._") or media_path.stem
        requested_source = normalize_language(source_lang)
        target = normalize_language(target_lang)
        game = str(game_name or "").strip() or self.default_game

        if progress:
            progress("Transcribing audio with faster-whisper...")
        audio_path = self.whisper.extract_audio(media_path)
        segments, detected = self.whisper.transcribe(audio_path, language=requested_source)
        source = requested_source or detected or self.default_language
        transcript = " ".join(
            str(segment.get("text") or "").strip() for segment in segments
        ).strip()

        if progress:
            progress(f"Generating native {source.upper()} subtitles and metadata...")
        native_metadata, native_source = self.localizer.native_metadata(
            transcript, source, game
        )
        native_ass = WORKSPACE_TEMP_DIR / f"{clip_id}_{source}.ass"
        self.ass_generator.write(segments, native_ass, lang=source)
        variants: dict[str, dict[str, Any]] = {
            source: {
                "lang": source,
                "segments": segments,
                "ass_path": str(native_ass),
                "metadata": native_metadata,
                "mode": "native",
                "source": native_source,
            }
        }

        if target and target != source:
            if progress:
                progress(f"Translating subtitles and metadata into {target.upper()}...")
            translation = self.localizer.translate_segments(
                segments, source, target, game, native_metadata
            )
            translated_segments = [
                {**segment, "text": text}
                for segment, text in zip(
                    segments, translation.get("translations", []), strict=False
                )
            ]
            target_ass = WORKSPACE_TEMP_DIR / f"{clip_id}_{target}.ass"
            self.ass_generator.write(translated_segments, target_ass, lang=target)
            variants[target] = {
                "lang": target,
                "segments": translated_segments,
                "ass_path": str(target_ass),
                "metadata": translation.get("metadata") or build_template_metadata(
                    target, game, transcript
                ),
                "mode": "translated" if translation.get("translated") else "source-fallback",
                "source": str(translation.get("source") or "template"),
            }

        localization_path = WORKSPACE_TEMP_DIR / f"{clip_id}_localization.json"
        payload: dict[str, Any] = {
            "clip_id": clip_id,
            "media_path": str(media_path),
            "audio_path": str(audio_path),
            "game_name": game,
            "source_lang": source,
            "detected_language": detected,
            "transcript": transcript,
            "variants": variants,
            "localization_file": str(localization_path),
        }
        localization_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Subtitle processing complete for %s (%d segments, variants=%s)",
            clip_id,
            len(segments),
            ", ".join(variants),
        )
        return payload


def process_media(
    media_path: Path,
    clip_id: str,
    source_lang: str | None = "en",
    target_lang: str | None = None,
    game_name: str = "",
) -> dict[str, Any]:
    """Convenience wrapper used by the UI and smoke tests."""
    return SubtitleProcessor().process(
        media_path,
        clip_id,
        source_lang=source_lang,
        target_lang=target_lang,
        game_name=game_name,
    )
