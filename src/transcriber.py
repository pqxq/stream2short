import json
import logging
import os
from pathlib import Path
from typing import Any

import openai
import torch
import yaml
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

WIDTH = config["width"]
HEIGHT = config["height"]
WHISPER_MODEL_NAME = config["whisper_model"]
COMPUTE_TYPE = config["compute_type"]
FONT_FAMILY = config["font_family"]
FONT_SIZE = config["font_size"]
PRIMARY_COLOR = config["primary_color"]
OUTLINE_THICKNESS = config["outline_thickness"]
MARGIN_V = config["margin_v"]

MAX_LINE_LENGTH = 32

DATA_DIR = Path("data")
WORKSPACE_TEMP_DIR = Path("workspace/temp")

def load_config() -> dict:
    return config

def ensure_dirs():
    WORKSPACE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

class WhisperTranscriber:
    def __init__(self, model_name: str = WHISPER_MODEL_NAME, compute_type: str = COMPUTE_TYPE):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        logger.info("Whisper model loaded on %s", device)

    def transcribe(self, audio_path: Path) -> list[dict[str, Any]]:
        """Transcribe audio file using faster-whisper.
        Returns list of segments with start, end, text.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        try:
            segments, _ = self.model.transcribe(str(audio_path), language="en", word_timestamps=False)
            result = []
            for segment in segments:
                result.append({
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip(),
                })
            logger.info("Transcribed %d segments from %s", len(result), audio_path.name)
            return result
        except Exception as e:
            logger.error("Whisper transcription failed: %s", e)
            raise

class ASSGenerator:
    def __init__(self, config: dict):
        self.width = config["width"]
        self.height = config["height"]
        self.font_family = config["font_family"]
        self.font_size = config["font_size"]
        self.primary_color = config["primary_color"]
        self.outline_thickness = config["outline_thickness"]
        self.margin_v = config["margin_v"]

    def wrap_text(self, text: str) -> list[str]:
        """Wrap text to max 32 characters, break at word boundaries using \\N."""
        if len(text) <= MAX_LINE_LENGTH:
            return [text]
        words = text.split()
        lines = []
        current_line = ""
        for word in words:
            if not current_line:
                candidate = word
            else:
                candidate = f"{current_line} {word}"
            if len(candidate) <= MAX_LINE_LENGTH:
                current_line = candidate
            else:
                if current_line:
                    lines.append(current_line)
                    current_line = word
                else:
                    lines.append(word)
        if current_line:
            lines.append(current_line)
        return lines

    def segments_to_ass(self, segments: list[dict[str, Any]]) -> str:
        """Generate ASS subtitle content from segments."""
        lines = []
        lines.append("[Script Info]")
        lines.append("Title: GTA6 Clip Factory")
        lines.append("ScriptType: v4.00+")
        lines.append(f"PlayResX: {self.width}")
        lines.append(f"PlayResY: {self.height}")
        lines.append("\n[Fonts]")
        lines.append(f"FontName: {self.font_family}")
        lines.append("\n[Events]")
        lines.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")

        for idx, seg in enumerate(segments, start=1):
            start_time = self._format_time(seg["start"])
            end_time = self._format_time(seg["end"])
            text = seg["text"]
            wrapped = self.wrap_text(text)
            for line in wrapped:
                escaped_line = line.replace("\\", "\\\\").replace("{", "{{").replace("}", "}}")
                lines.append(f"Dialogue: 0,{start_time},{end_time},Default,,0,0,{self.margin_v},,{{{escaped_line}}}")
        return "\n".join(lines)

    @staticmethod
    def _format_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        cs = int((s - int(s)) * 100)
        s = int(s)
        return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"

class TranslationHookGenerator:
    def __init__(self, llm_config: dict):
        self.llm_url = llm_config.get("LLM_BASE_URL")
        self.llm_key = llm_config.get("LLM_API_KEY")
        self.llm_model = llm_config.get("LLM_MODEL")
        self.use_llm = bool(self.llm_url and self.llm_key)
        if self.use_llm:
            self.client = openai.OpenAI(api_key=self.llm_key, base_url=self.llm_url)
        logger.info("LLM configured: %s", "yes" if self.use_llm else "fallback to deep-translator")

    def translate_and_hooks(self, text: str) -> dict[str, Any]:
        """Translate text to Russian and generate viral hook headlines for EN and RU."""
        translation = None
        en_hook = None
        ru_hook = None

        if self.use_llm:
            try:
                sys_prompt = (
                    "You are a video subtitle translator and viral hook generator for gaming clips. "
                    "Translate the following English subtitle into idiomatic Russian suitable for gamers. "
                    "Also generate two viral hook headlines: one in English (~45 chars) and one in Russian (~45 chars). "
                    "Return JSON with fields: translation (Russian), en_hook, ru_hook."
                )
                resp = self.client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": text}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.7,
                    max_tokens=150,
                )
                out = resp.choices[0].message.content
                data = json.loads(out)
                translation = data.get("translation")
                en_hook = data.get("en_hook")
                ru_hook = data.get("ru_hook")
                logger.info("LLM translation and hooks generated.")
            except Exception as e:
                logger.warning("LLM translation failed (%s), falling back to deep-translator.", e)
                self.use_llm = False

        if not self.use_llm:
            try:
                translator = GoogleTranslator(source="auto", target="ru")
                translation = translator.translate(text)
                logger.info("Deep-translator translation: %s", translation[:50])
            except Exception as e:
                logger.error("Deep-translator also failed: %s", e)
                translation = "[Translation unavailable]"

        if not en_hook:
            en_hook = self._generate_simple_hook(text, lang="en")
        if not ru_hook and translation and translation != "[Translation unavailable]":
            ru_hook = self._generate_simple_hook(translation, lang="ru")

        return {
            "translation": translation,
            "en_hook": en_hook,
            "ru_hook": ru_hook,
        }

    def _generate_simple_hook(self, text: str, lang: str) -> str:
        """Simple template-based hook if LLM unavailable."""
        words = text.split()
        if len(words) > 4:
            key_phrase = " ".join(words[:4])
        else:
            key_phrase = text
        templates = {
            "en": f"Can't believe this {key_phrase}!!!",
            "ru": f"Представляете, {key_phrase}? Это фаново!",
        }
        return templates.get(lang, key_phrase)

class SubtitleProcessor:
    def __init__(self, config: dict):
        self.config = config
        self.whisper = WhisperTranscriber()
        self.ass_gen = ASSGenerator(config)
        self.translator_hooks = TranslationHookGenerator({
            "LLM_BASE_URL": os.getenv("LLM_BASE_URL"),
            "LLM_API_KEY": os.getenv("LLM_API_KEY"),
            "LLM_MODEL": os.getenv("LLM_MODEL"),
        })

    def process(self, audio_path: Path) -> dict[str, Any]:
        ensure_dirs()
        # Transcribe
        segments = self.whisper.transcribe(audio_path)
        # Generate ASS content
        ass_content = self.ass_gen.segments_to_ass(segments)
        # Save ASS to workspace/temp/<clip_id>.ass
        ass_file = WORKSPACE_TEMP_DIR / f"{audio_path.stem}.ass"
        with open(ass_file, "w", encoding="utf-8") as f:
            f.write(ass_content)
        # Generate translation and hooks from entire transcribed text
        full_text = " ".join(seg["text"] for seg in segments)
        translation_data = self.translator_hooks.translate_and_hooks(full_text)
        # Save translation_data JSON for later use
        data_file = WORKSPACE_TEMP_DIR / f"{audio_path.stem}_translation.json"
        with open(data_file, "w", encoding="utf-8") as f:
            json.dump(translation_data, f, indent=2)
        logger.info("Subtitle processing complete: %s", audio_path.name)
        return {
            "segments": segments,
            "ass_path": str(ass_file),
            "translation": translation_data,
        }

if __name__ == "__main__":
    # Quick test: process a dummy audio file if exists
    import sys
    if len(sys.argv) > 1:
        audio = Path(sys.argv[1])
        processor = SubtitleProcessor(load_config())
        processor.process(audio)
    else:
        logger.info("Usage: python src/transcriber.py <audio_path>")
