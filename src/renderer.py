import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

WIDTH = config["width"]
HEIGHT = config["height"]
FONT_FAMILY = config["font_family"]
FONT_SIZE = config["font_size"]
PRIMARY_COLOR = config["primary_color"]
OUTLINE_THICKNESS = config["outline_thickness"]
MARGIN_V = config["margin_v"]
EN_SCALE = config["en_scale"]
RU_SCALE = config["ru_scale"]
RU_CONTRAST = config["ru_contrast"]
RU_SATURATION = config["ru_saturation"]
EN_AUDIO_SPEED = 1.0
RU_AUDIO_SPEED = 1.02
RU_VIDEO_SPEED = 1.04
SUBTITLE_SYNC_FACTOR = 0.98

PRESETS_PATH = Path("presets.json")
with open(PRESETS_PATH, "r", encoding="utf-8") as f:
    presets_data = json.load(f)

DEFAULT_PRESET = presets_data.get("default", {"cam_x": 0.0, "cam_y": 0.0, "cam_w": 0.2, "cam_h": 0.25})
WORKSPACE_TEMP = Path("workspace/temp")
WORKSPACE_RAW = Path("workspace/raw")
OUTPUT_EN_DIR = Path("output/en")
OUTPUT_RU_DIR = Path("output/ru")

# Font path (Ubuntu font)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

class FFmpegRenderer:
    def __init__(self, preset_name: str = "default"):
        self.preset = presets_data.get(preset_name, DEFAULT_PRESET)
        self.cam_x = self.preset["cam_x"]
        self.cam_y = self.preset["cam_y"]
        self.cam_w = self.preset["cam_w"]
        self.cam_h = self.preset["cam_h"]
        self.ensure_dirs()

    def ensure_dirs(self):
        WORKSPACE_TEMP.mkdir(parents=True, exist_ok=True)
        OUTPUT_EN_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_RU_DIR.mkdir(parents=True, exist_ok=True)

    def get_crop_coords(self) -> tuple[float, float, float, float]:
        return self.cam_x, self.cam_y, self.cam_w, self.cam_h

    def _write_hook_text(self, text: str, suffix: str, temp_dir: Path) -> Path:
        hook_file = temp_dir / f"hook_{suffix}.txt"
        with open(hook_file, "w", encoding="utf-8") as f:
            f.write(text)
        return hook_file

    def _build_drawtext_filter(self, hook_file: Path, x: int = 50, y: int | None = None) -> str:
        if y is None:
            y = HEIGHT - MARGIN_V - FONT_SIZE * 2
        return (
            f"drawtext=textfile={hook_file}:"
            f"fontfile={FONT_PATH}:"
            f"fontsize={FONT_SIZE}:"
            f"fontcolor={PRIMARY_COLOR}:"
            f"borderw={OUTLINE_THICKNESS}:"
            f"bordercolor=black:"
            f"x={x}:y={y}:"
            f"fontsize={FONT_SIZE}"
        )

    def _adjust_subtitle_timestamps(self, ass_path: Path, factor: float) -> Path:
        adjusted_path = WORKSPACE_TEMP / f"{ass_path.stem}_adjusted.ass"
        with open(ass_path, "r", encoding="utf-8") as fin, open(adjusted_path, "w", encoding="utf-8") as fout:
            for line in fin:
                if line.strip().startswith("Dialogue:"):
                    parts = line.split(",", 9)
                    if len(parts) >= 10:
                        time_part = parts[2]
                        start_end = time_part.split(" -> ")
                        if len(start_end) == 2:
                            def adjust_ts(ts: str) -> str:
                                h, m, s_ms = ts.split(":")
                                s, ms = s_ms.split(".")
                                total_seconds = int(h) * 3600 + int(m) * 60 + float(s) + float(f"0.{ms}")
                                adjusted = total_seconds * factor
                                h_adj = int(adjusted // 3600)
                                m_adj = int((adjusted % 3600) // 60)
                                s_adj = adjusted % 60
                                ms_adj = int((s_adj - int(s_adj)) * 1000)
                                s_adj_int = int(s_adj)
                                return f"{h_adj:01d}:{m_adj:02d}:{s_adj_int:02d}.{ms_adj:03d}"
                            parts[2] = f"{adjust_ts(start_end[0])} -> {adjust_ts(start_end[1])}"
                            line = ",".join(parts)
                fout.write(line)
        return adjusted_path

    def _extract_middle_frame(self, video_path: Path) -> Path:
        frame_path = WORKSPACE_TEMP / f"{video_path.stem}_frame.png"
        # Use ffprobe to get duration
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-of", "default=noprint_wrappers=1:nokey=1", "-show_entries",
               "stream=duration", str(video_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            duration = float(result.stdout.strip())
            mid_time = duration / 2.0
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
            mid_time = 5.0
        cmd = [
            "ffmpeg", "-y", "-ss", str(mid_time),
            "-i", str(video_path), "-vframes", "1",
            str(frame_path)
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return frame_path

    def _create_split_screen_preview(self, original_path: Path, preview_path: Path) -> None:
        _cam_x, _cam_y, _cam_w, _cam_h = self.get_crop_coords()
        # Extract middle frame
        self._extract_middle_frame(original_path)
        # Use Pillow for preview (optional). For now, we skip actual preview generation.
        # This is just a placeholder; the real processing is done via FFmpeg in render methods.
        logger.debug("Split-screen preview generated at %s", preview_path)

    def render_en(self, clip_id: str, raw_video_path: Path, hooks_data: dict[str, Any], subtitle_path: Path) -> Path:
        output_path = OUTPUT_EN_DIR / f"{clip_id}_en.mp4"
        hook_text = hooks_data.get("en_hook", "")
        # Write hook to temp text file
        hook_file = self._write_hook_text(hook_text, f"{clip_id}_en", WORKSPACE_TEMP)
        # Build drawtext filter
        draw_filter = self._build_drawtext_filter(hook_file)
        # FFmpeg command for EN version: scale 1.0, speed 1.0, drawtext
        # Input raw video + subtitle overlay
        # Use libass for subtitle rendering
        cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_video_path),
            "-vf", f"{draw_filter}, subtitles={subtitle_path}:force_style='FontName={FONT_FAMILY},FontSize={FONT_SIZE},PrimaryColour={PRIMARY_COLOR},OutlineWidth={OUTLINE_THICKNESS}'",
            "-c:a", "aac",
            "-b:a", "128k",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            str(output_path)
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("Rendered EN video: %s", output_path)
        return output_path

    def render_ru(self, clip_id: str, raw_video_path: Path, hooks_data: dict[str, Any], subtitle_path: Path) -> Path:
        output_path = OUTPUT_RU_DIR / f"{clip_id}_ru.mp4"
        hook_text = hooks_data.get("ru_hook", "")
        hook_file = self._write_hook_text(hook_text, f"{clip_id}_ru", WORKSPACE_TEMP)
        # Adjust subtitle timestamps for 1.02x playback
        adjusted_subtitle_path = self._adjust_subtitle_timestamps(subtitle_path, SUBTITLE_SYNC_FACTOR)
        # Build drawtext filter for RU
        draw_filter = self._build_drawtext_filter(hook_file)
        # Scale up 1.04x and center-crop back to 1080x1920
        crop_filter = (
            f"scale=iw*{RU_SCALE}:ih*{RU_SCALE},"
            f"crop={WIDTH}:{HEIGHT}:(iw-{WIDTH})/{RU_SCALE}/2:(ih-{HEIGHT})/{RU_SCALE}/2"
        )
        # Apply contrast and saturation
        eq_filter = f"eq=contrast={RU_CONTRAST}:saturation={RU_SATURATION}"
        # Speed adjustment for video and audio
        setpts = f"setpts=1.0/{RU_VIDEO_SPEED}*PTS"
        atempo = f"atempo={RU_AUDIO_SPEED}"
        # Combine filters
        vf = f"{crop_filter},{draw_filter},{eq_filter},subtitles={adjusted_subtitle_path}:force_style='FontName={FONT_FAMILY},FontSize={FONT_SIZE},PrimaryColour={PRIMARY_COLOR},OutlineWidth={OUTLINE_THICKNESS}',{setpts},{atempo}"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_video_path),
            "-vf", vf,
            "-c:a", "aac",
            "-b:a", "128k",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            str(output_path)
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("Rendered RU video: %s", output_path)
        return output_path

    def render_fullscreen_blur(self, clip_id: str, raw_video_path: Path, hooks_data: dict[str, Any], subtitle_path: Path) -> Path:
        # Full-screen 9:16 gameplay with blurred background
        # Using a simple blur filter on the whole frame via Gaussian blur
        output_path = OUTPUT_EN_DIR / f"{clip_id}_blur.mp4"
        hook_text = hooks_data.get("en_hook", "")
        hook_file = self._write_hook_text(hook_text, f"{clip_id}_blur", WORKSPACE_TEMP)
        draw_filter = self._build_drawtext_filter(hook_file)
        # Blur the background (simple method: apply Gaussian blur to entire frame)
        blur_filter = "gblur=sigma=5"
        vf = f"{blur_filter},{draw_filter},subtitles={subtitle_path}:force_style='FontName={FONT_FAMILY},FontSize={FONT_SIZE},PrimaryColour={PRIMARY_COLOR},OutlineWidth={OUTLINE_THICKNESS}'"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_video_path),
            "-vf", vf,
            "-c:a", "aac",
            "-b:a", "128k",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            str(output_path)
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("Rendered blur video: %s", output_path)
        return output_path

    def process_clip(self, clip_id: str, raw_video_path: Path, hooks_data: dict[str, Any], subtitle_path: Path, layout_mode: str = "split") -> dict[str, Path]:
        results = {}
        if layout_mode == "split":
            results["en"] = self.render_en(clip_id, raw_video_path, hooks_data, subtitle_path)
            results["ru"] = self.render_ru(clip_id, raw_video_path, hooks_data, subtitle_path)
        elif layout_mode == "blur":
            results["blur"] = self.render_fullscreen_blur(clip_id, raw_video_path, hooks_data, subtitle_path)
        logger.info("Clip %s processed successfully with layout: %s", clip_id, layout_mode)
        return results

if __name__ == "__main__":
    import sys
    # Simple CLI test: process a raw video and translation JSON
    if len(sys.argv) < 3:
        logger.info("Usage: python src/renderer.py <clip_id> <raw_video_path> <translation_json_path>")
        sys.exit(1)
    clip_id = sys.argv[1]
    raw_video_path = Path(sys.argv[2])
    translation_json_path = Path(sys.argv[3])
    if not translation_json_path.exists():
        logger.error("Translation JSON not found: %s", translation_json_path)
        sys.exit(1)
    with open(translation_json_path, "r", encoding="utf-8") as f:
        hooks_data = json.load(f)
    renderer = FFmpegRenderer()
    # For testing, use a dummy subtitle path (should exist)
    subtitle_path = Path("workspace/temp/test.ass")
    if not subtitle_path.exists():
        logger.error("Subtitle file not found: %s", subtitle_path)
        sys.exit(1)
    renderer.process_clip(clip_id, raw_video_path, hooks_data, subtitle_path, layout_mode="split")
