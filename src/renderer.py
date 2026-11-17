"""FFmpeg compositor for vertical 9:16 clips in any language.

Every dynamic banner string is written to a UTF-8 text file and passed to
``drawtext`` via ``textfile=``; no user text is ever interpolated into a
filtergraph.  Anti-duplicate fingerprinting (zoom, colour grade and speed) is
optional and applies equally to every language.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.languages import normalize_language, validate_language_code

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
PRESETS_PATH = PROJECT_ROOT / "presets.json"
WORKSPACE_TEMP = PROJECT_ROOT / "workspace" / "temp"
WORKSPACE_RAW = PROJECT_ROOT / "workspace" / "raw"
OUTPUT_ROOT = PROJECT_ROOT / "output"

DEFAULT_PRESET = {"cam_x": 0.0, "cam_y": 0.0, "cam_w": 0.2, "cam_h": 0.25}
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
CJK_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
CJK_FONT_FAMILIES = {"ja": "Noto Sans CJK JP", "ko": "Noto Sans CJK KR"}

DEFAULT_CONFIG: dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "font_family": "DejaVu Sans",
    "font_size": 36,
    "primary_color": "#FFFF00",
    "outline_thickness": 2,
    "margin_v": 220,
    "anti_duplicate_enabled": True,
    "anti_duplicate_scale": 1.04,
    "anti_duplicate_contrast": 1.03,
    "anti_duplicate_saturation": 1.05,
    "anti_duplicate_speed": 1.02,
    "audio_sample_rate": 44100,
}

CAM_PANEL_HEIGHT = 640


def load_config() -> dict[str, Any]:
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


def load_presets() -> dict[str, dict[str, float]]:
    if not PRESETS_PATH.exists():
        return {"default": dict(DEFAULT_PRESET)}
    try:
        with PRESETS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", PRESETS_PATH, exc)
        return {"default": dict(DEFAULT_PRESET)}
    presets: dict[str, dict[str, float]] = {}
    if isinstance(data, dict):
        for name, values in data.items():
            if isinstance(values, dict):
                presets[str(name)] = {
                    "cam_x": float(values.get("cam_x", 0.0)),
                    "cam_y": float(values.get("cam_y", 0.0)),
                    "cam_w": float(values.get("cam_w", 0.2)),
                    "cam_h": float(values.get("cam_h", 0.25)),
                }
    if "default" not in presets:
        presets["default"] = dict(DEFAULT_PRESET)
    return presets


def save_preset(
    streamer: str, cam_x: float, cam_y: float, cam_w: float, cam_h: float
) -> dict[str, dict[str, float]]:
    """Persist a webcam crop preset and return the updated preset map."""
    presets = load_presets()
    key = (streamer or "default").strip() or "default"
    presets[key] = {
        "cam_x": max(0.0, min(1.0, float(cam_x))),
        "cam_y": max(0.0, min(1.0, float(cam_y))),
        "cam_w": max(0.01, min(1.0, float(cam_w))),
        "cam_h": max(0.01, min(1.0, float(cam_h))),
    }
    PRESETS_PATH.write_text(
        json.dumps(presets, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return presets


def font_family_for(lang_code: str | None) -> str:
    code = normalize_language(lang_code)
    if code in CJK_FONT_FAMILIES and any(Path(path).exists() for path in CJK_FONT_CANDIDATES):
        return CJK_FONT_FAMILIES[code]
    return "DejaVu Sans"


def resolve_font_path(lang_code: str | None = None) -> str:
    code = normalize_language(lang_code)
    if code in CJK_FONT_FAMILIES:
        for candidate in CJK_FONT_CANDIDATES:
            if Path(candidate).exists():
                return candidate
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return FONT_CANDIDATES[0]


def output_dir_for(lang_code: str) -> Path:
    return OUTPUT_ROOT / validate_language_code(lang_code)


def _even(value: float, upper: int | None = None) -> int:
    rounded = max(int(value / 2.0) * 2, 2)
    if upper is not None:
        rounded = min(rounded, upper if upper % 2 == 0 else upper - 1)
    return max(2, rounded)


def _filter_path(path: Path) -> str:
    text = str(Path(path).resolve())
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


def _hex_to_ass_bgr(hex_color: str) -> str:
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        value = "FFFF00"
    try:
        red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        red, green, blue = 255, 255, 0
    return f"&H00{blue:02X}{green:02X}{red:02X}&"


def _parse_ass_timestamp(value: str) -> float:
    match = re.match(r"^(\d+):(\d{2}):(\d{2})[.](\d{2,3})$", value.strip())
    if not match:
        return 0.0
    hours, minutes, seconds, fraction = match.groups()
    divisor = 100.0 if len(fraction) == 2 else 1000.0
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / divisor


def _format_ass_timestamp(seconds: float) -> str:
    safe = max(0.0, seconds)
    hours = int(safe // 3600)
    minutes = int((safe % 3600) // 60)
    whole = int(safe % 60)
    centiseconds = min(99, round((safe - int(safe)) * 100))
    return f"{hours}:{minutes:02d}:{whole:02d}.{centiseconds:02d}"


def adjust_ass_timestamps(ass_path: Path, factor: float, suffix: str = "adjusted") -> Path:
    """Copy an ASS file with all Dialogue timestamps multiplied by ``factor``."""
    source = Path(ass_path)
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", suffix) or "adjusted"
    target = source.with_name(f"{source.stem}_{safe_suffix}.ass")
    pattern = re.compile(r"^(Dialogue:\s*[^,]*,)([^,]+),([^,]+),(.*)$")
    output_lines: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if not match:
            output_lines.append(line)
            continue
        start = _format_ass_timestamp(_parse_ass_timestamp(match.group(2)) * factor)
        end = _format_ass_timestamp(_parse_ass_timestamp(match.group(3)) * factor)
        output_lines.append(f"{match.group(1)}{start},{end},{match.group(4)}")
    target.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return target


class FFmpegRenderer:
    """Build split-screen or blurred 9:16 clips with optional fingerprinting."""

    def __init__(
        self,
        preset_name: str = "default",
        cam_box: tuple[float, float, float, float] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.presets = load_presets()
        preset = self.presets.get(preset_name, self.presets["default"])
        if cam_box is not None:
            preset = {
                "cam_x": cam_box[0],
                "cam_y": cam_box[1],
                "cam_w": cam_box[2],
                "cam_h": cam_box[3],
            }
        self.preset_name = preset_name
        self.cam_x = max(0.0, min(1.0, float(preset["cam_x"])))
        self.cam_y = max(0.0, min(1.0, float(preset["cam_y"])))
        self.cam_w = max(0.01, min(1.0, float(preset["cam_w"])))
        self.cam_h = max(0.01, min(1.0, float(preset["cam_h"])))
        self.width = _even(float(self.config["width"]))
        self.height = _even(float(self.config["height"]))
        self.font_family = str(self.config["font_family"])
        self.font_size = int(self.config["font_size"])
        self.primary_color = str(self.config["primary_color"])
        self.outline_thickness = int(self.config["outline_thickness"])
        self.margin_v = int(self.config["margin_v"])
        self.anti_duplicate_enabled = bool(self.config.get("anti_duplicate_enabled", True))
        self.anti_duplicate_scale = max(
            1.0, float(self.config.get("anti_duplicate_scale", 1.04))
        )
        self.anti_duplicate_contrast = float(self.config.get("anti_duplicate_contrast", 1.03))
        self.anti_duplicate_saturation = float(self.config.get("anti_duplicate_saturation", 1.05))
        self.anti_duplicate_speed = float(self.config.get("anti_duplicate_speed", 1.02))
        if self.anti_duplicate_speed <= 0:
            self.anti_duplicate_speed = 1.02
        self.subtitle_factor = 1.0 / self.anti_duplicate_speed
        self.audio_sample_rate = int(self.config.get("audio_sample_rate", 44100))
        self.ensure_dirs()

    @property
    def cam_box(self) -> tuple[float, float, float, float]:
        return self.cam_x, self.cam_y, self.cam_w, self.cam_h

    def ensure_dirs(self) -> None:
        for directory in (WORKSPACE_TEMP, WORKSPACE_RAW, OUTPUT_ROOT):
            directory.mkdir(parents=True, exist_ok=True)

    def probe(self, video_path: Path) -> dict[str, Any]:
        """Return width/height/duration/audio information via ffprobe."""
        command = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("ffprobe is not installed or not on PATH.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ffprobe failed: {(exc.stderr or '').strip()}") from exc
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams", [])
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"), {}
        )
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        duration = 0.0
        for candidate in (payload.get("format", {}).get("duration"), video_stream.get("duration")):
            try:
                duration = float(candidate)
            except (TypeError, ValueError):
                continue
            if duration > 0:
                break
        return {
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
            "duration": duration,
            "has_audio": has_audio,
            "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        }

    def _source_crop(self, probe: dict[str, Any]) -> tuple[int, int, int, int]:
        source_width = int(probe.get("width") or 1280)
        source_height = int(probe.get("height") or 720)
        crop_width = _even(source_width * self.cam_w, source_width)
        crop_height = _even(source_height * self.cam_h, source_height)
        crop_x = _even(source_width * self.cam_x, max(0, source_width - crop_width))
        crop_y = _even(source_height * self.cam_y, max(0, source_height - crop_height))
        crop_x = min(crop_x, max(0, source_width - crop_width))
        crop_y = min(crop_y, max(0, source_height - crop_height))
        return crop_width, crop_height, crop_x, crop_y

    def _ass_force_style(self, font_family: str) -> str:
        return (
            f"FontName={font_family},FontSize={self.font_size},"
            f"PrimaryColour={_hex_to_ass_bgr(self.primary_color)},"
            f"SecondaryColour={_hex_to_ass_bgr(self.primary_color)},"
            "OutlineColour=&H00000000&,BackColour=&H00000000&,"
            f"Bold=1,Outline={self.outline_thickness},Shadow=0,Alignment=2,"
            f"MarginL=100,MarginR=100,MarginV={self.margin_v},Encoding=1"
        )

    def _font_family_for(self, lang_code: str | None) -> str:
        code = normalize_language(lang_code)
        if code in CJK_FONT_FAMILIES and any(
            Path(path).exists() for path in CJK_FONT_CANDIDATES
        ):
            return CJK_FONT_FAMILIES[code]
        return self.font_family

    def _subtitle_filter(self, ass_path: Path, font_family: str) -> str:
        return (
            f"subtitles='{_filter_path(ass_path)}':"
            f"force_style='{self._ass_force_style(font_family)}'"
        )

    def write_hook_textfile(self, text: str, suffix: str) -> Path:
        safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", suffix)
        target = WORKSPACE_TEMP / f"hook_{safe_suffix}.txt"
        banner = re.sub(r"\s+", " ", text or "").strip() or "VIRAL MOMENT"
        target.write_text(banner, encoding="utf-8")
        return target

    def build_drawtext_filter(
        self,
        hook_file: Path,
        font_path: Path,
        x_expr: str = "(w-text_w)/2",
        y_expr: str = "120",
    ) -> str:
        hook_size = self.font_size + 10
        return (
            f"drawtext=textfile='{_filter_path(hook_file)}':"
            f"fontfile='{_filter_path(font_path)}':"
            f"fontsize={hook_size}:fontcolor=white:borderw=3:bordercolor=black:"
            f"x={x_expr}:y={y_expr}:fix_bounds=true:expansion=none"
        )

    def _composed_video_filter(
        self,
        clip_id: str,
        probe: dict[str, Any],
        hook_text: str,
        ass_path: Path,
        lang_code: str,
        layout: str,
        apply_uniqueness: bool,
    ) -> tuple[str, Path, Path]:
        cam_w, cam_h, cam_x, cam_y = self._source_crop(probe)
        game_height = self.height - CAM_PANEL_HEIGHT
        hook_file = self.write_hook_textfile(hook_text, f"{clip_id}_{lang_code}")
        font_family = self._font_family_for(lang_code)
        font_path = Path(resolve_font_path(lang_code))

        if layout == "blur":
            graph = (
                f"[0:v]split=2[bgs][fgs];"
                f"[bgs]scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{self.height},gblur=sigma=20[bg];"
                f"[fgs]scale={self.width}:{self.height}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
            )
        else:
            graph = (
                f"[0:v]split=2[vc][vg];"
                f"[vc]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
                f"scale={self.width}:{CAM_PANEL_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{CAM_PANEL_HEIGHT},"
                f"pad={self.width}:{self.height}:0:0:black[cam];"
                f"[vg]scale={self.width}:{game_height}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{game_height}[game];"
                f"[cam][game]overlay=0:{CAM_PANEL_HEIGHT}:format=auto[base]"
            )

        if apply_uniqueness:
            zoom_width = _even(self.width * self.anti_duplicate_scale)
            zoom_height = _even(self.height * self.anti_duplicate_scale)
            adjusted_ass = adjust_ass_timestamps(
                ass_path, self.subtitle_factor, suffix=f"{lang_code}_uniq"
            )
            setpts_factor = f"{self.subtitle_factor:.6f}"
            graph += (
                f";[base]scale={zoom_width}:{zoom_height},"
                f"crop={self.width}:{self.height},"
                f"eq=contrast={self.anti_duplicate_contrast}:"
                f"saturation={self.anti_duplicate_saturation},"
                f"setpts={setpts_factor}*PTS[graded]"
            )
            final_ass = adjusted_ass
        else:
            graph += ";[base]null[graded]"
            final_ass = Path(ass_path)

        graph += (
            f";[graded]{self.build_drawtext_filter(hook_file, font_path)}[hooked];"
            f"[hooked]{self._subtitle_filter(final_ass, font_family)}[vout]"
        )
        logger.debug("Filtergraph for %s/%s: %s", clip_id, lang_code, graph)
        return graph, hook_file, final_ass

    def _run_render(
        self,
        clip_id: str,
        raw_video_path: Path,
        hook_text: str,
        ass_path: Path,
        lang_code: str,
        layout: str,
        apply_uniqueness: bool,
    ) -> Path:
        raw_video_path = Path(raw_video_path)
        if not raw_video_path.exists():
            raise FileNotFoundError(f"Raw video not found: {raw_video_path}")
        if not Path(ass_path).exists():
            raise FileNotFoundError(f"Subtitle file not found: {ass_path}")
        code = validate_language_code(lang_code)
        probe = self.probe(raw_video_path)
        if not probe["width"] or not probe["height"]:
            raise RuntimeError(f"Could not determine video dimensions for {raw_video_path}")

        effective_uniqueness = bool(apply_uniqueness and self.anti_duplicate_enabled)
        graph, _hook_file, _final_ass = self._composed_video_filter(
            clip_id,
            probe,
            hook_text,
            Path(ass_path),
            code,
            layout,
            effective_uniqueness,
        )
        output_dir = output_dir_for(code)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{clip_id}_{code}.mp4"

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_video_path),
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            str(self.audio_sample_rate),
            "-movflags",
            "+faststart",
        ]
        if probe.get("has_audio"):
            atempo = self.anti_duplicate_speed if effective_uniqueness else 1.0
            command.extend(["-af", f"atempo={atempo:.6f}" if atempo != 1.0 else "anull"])
            command.append("-shortest")
        command.append(str(output_path))

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg is not installed or not on PATH.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().splitlines()
            detail = "\n".join(stderr[-8:]) if stderr else "unknown ffmpeg error"
            raise RuntimeError(
                f"ffmpeg render failed for {clip_id}/{code}:\n{detail}"
            ) from exc
        logger.info(
            "Rendered %s %s (uniqueness=%s) -> %s",
            clip_id,
            code.upper(),
            effective_uniqueness,
            output_path,
        )
        return output_path

    def render_clip(
        self,
        clip_id: str,
        raw_video_path: Path,
        hook_text: str,
        ass_path: Path,
        lang_code: str,
        layout: str = "split",
        apply_uniqueness: bool = True,
    ) -> Path:
        """Render one language variant to ``output/<lang>/<clip>_<lang>.mp4``."""
        return self._run_render(
            clip_id,
            raw_video_path,
            hook_text,
            ass_path,
            lang_code,
            layout,
            apply_uniqueness,
        )

    def render_split_screen(
        self,
        clip_id: str,
        raw_video_path: Path,
        hook_text: str,
        ass_path: Path,
        lang_code: str = "en",
        apply_uniqueness: bool = True,
    ) -> Path:
        return self.render_clip(
            clip_id,
            raw_video_path,
            hook_text,
            ass_path,
            lang_code,
            layout="split",
            apply_uniqueness=apply_uniqueness,
        )

    def render_blurred_fullscreen(
        self,
        clip_id: str,
        raw_video_path: Path,
        hook_text: str,
        ass_path: Path,
        lang_code: str = "en",
        apply_uniqueness: bool = True,
    ) -> Path:
        return self.render_clip(
            clip_id,
            raw_video_path,
            hook_text,
            ass_path,
            lang_code,
            layout="blur",
            apply_uniqueness=apply_uniqueness,
        )

    def extract_middle_frame(self, video_path: Path) -> Path:
        video_path = Path(video_path)
        self.ensure_dirs()
        duration = self.probe(video_path).get("duration") or 0.0
        frame_path = WORKSPACE_TEMP / f"{video_path.stem}_preview_frame.png"
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{max(0.0, duration / 2.0):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(frame_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Frame extraction failed: {(exc.stderr or '').strip()}") from exc
        return frame_path

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        try:
            return ImageFont.truetype(resolve_font_path(), size=size)
        except OSError:
            return ImageFont.load_default()

    @staticmethod
    def _cover_resize(image: Image.Image, width: int, height: int) -> Image.Image:
        source_ratio = image.width / max(1, image.height)
        target_ratio = width / max(1, height)
        if source_ratio > target_ratio:
            new_height = height
            new_width = max(1, round(height * source_ratio))
        else:
            new_width = width
            new_height = max(1, round(width / source_ratio))
        resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        left = (new_width - width) // 2
        top = (new_height - height) // 2
        return resized.crop((left, top, left + width, top + height))

    @staticmethod
    def _contain_resize(image: Image.Image, width: int, height: int) -> Image.Image:
        ratio = min(width / max(1, image.width), height / max(1, image.height))
        new_size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        return image.resize(new_size, Image.Resampling.LANCZOS)

    def preview_source(
        self, frame_path: Path, cam_box: tuple[float, float, float, float]
    ) -> Image.Image:
        """Original frame with green webcam and blue gameplay rectangles."""
        image = Image.open(frame_path).convert("RGB")
        draw = ImageDraw.Draw(image)
        cam_x, cam_y, cam_w, cam_h = cam_box
        green_box = (
            int(cam_x * image.width),
            int(cam_y * image.height),
            int((cam_x + cam_w) * image.width),
            int((cam_y + cam_h) * image.height),
        )
        draw.rectangle((1, 1, image.width - 2, image.height - 2), outline=(0, 120, 255), width=6)
        draw.rectangle(green_box, outline=(0, 255, 0), width=6)
        return image

    def preview_composite(
        self,
        frame_path: Path,
        cam_box: tuple[float, float, float, float],
        layout: str = "split",
        hook_text: str = "VIRAL MOMENT",
    ) -> Image.Image:
        """Render a 1080x1920 style preview with hook and subtitle placeholders."""
        source = Image.open(frame_path).convert("RGB")
        canvas = Image.new("RGB", (self.width, self.height), (12, 12, 18))
        cam_x, cam_y, cam_w, cam_h = cam_box
        cam_crop = source.crop(
            (
                int(cam_x * source.width),
                int(cam_y * source.height),
                int((cam_x + cam_w) * source.width),
                int((cam_y + cam_h) * source.height),
            )
        )

        if layout == "blur":
            background = self._cover_resize(source, self.width, self.height).filter(
                ImageFilter.GaussianBlur(radius=20)
            )
            canvas.paste(background, (0, 0))
            foreground = self._contain_resize(source, self.width, self.height)
            canvas.paste(
                foreground,
                ((self.width - foreground.width) // 2, (self.height - foreground.height) // 2),
            )
        else:
            canvas.paste(self._cover_resize(cam_crop, self.width, CAM_PANEL_HEIGHT), (0, 0))
            game_height = self.height - CAM_PANEL_HEIGHT
            canvas.paste(
                self._cover_resize(source, self.width, game_height),
                (0, CAM_PANEL_HEIGHT),
            )
            draw = ImageDraw.Draw(canvas)
            draw.line((0, CAM_PANEL_HEIGHT, self.width, CAM_PANEL_HEIGHT), fill=(255, 255, 255), width=4)

        draw = ImageDraw.Draw(canvas)
        hook_font = self._load_font(self.font_size + 12)
        subtitle_font = self._load_font(self.font_size)
        safe_hook = re.sub(r"\s+", " ", hook_text or "").strip() or "VIRAL MOMENT"
        hook_width = draw.textbbox((0, 0), safe_hook, font=hook_font)[2]
        draw.text(
            ((self.width - hook_width) / 2, 110),
            safe_hook,
            font=hook_font,
            fill=(255, 255, 255),
            stroke_width=4,
            stroke_fill=(0, 0, 0),
        )
        subtitle_text = "Sample subtitle placement"
        subtitle_width = draw.textbbox((0, 0), subtitle_text, font=subtitle_font)[2]
        draw.text(
            ((self.width - subtitle_width) / 2, self.height - self.margin_v),
            subtitle_text,
            font=subtitle_font,
            fill=self.primary_color,
            stroke_width=max(1, self.outline_thickness),
            stroke_fill=(0, 0, 0),
        )
        draw.rectangle((0, 0, self.width - 1, self.height - 1), outline=(80, 80, 90), width=2)
        return canvas


def render_clip(
    clip_id: str,
    raw_video_path: Path,
    hook_text: str,
    ass_path: Path,
    lang_code: str = "en",
    layout_mode: str = "split",
    preset_name: str = "default",
    apply_uniqueness: bool = True,
) -> Path:
    """Convenience wrapper used by the UI and smoke tests."""
    return FFmpegRenderer(preset_name=preset_name).render_clip(
        clip_id,
        raw_video_path,
        hook_text,
        ass_path,
        lang_code,
        layout=layout_mode,
        apply_uniqueness=apply_uniqueness,
    )
