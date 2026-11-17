"""GTA6 Clip Factory — Streamlit studio.

Run with ``streamlit run app.py``.  All expensive operations are triggered
explicitly from the UI; importing this module has no Streamlit side effects.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import streamlit as st
from dotenv import load_dotenv, set_key
from PIL import Image

from src.fetcher import ClipFetcher, fetch_twitch_clips, load_processed_clips
from src.renderer import (
    FFmpegRenderer,
    load_presets,
    save_preset,
)
from src.transcriber import SubtitleProcessor
from src.transcriber import load_config as load_base_config
from src.uploader import (
    CLIENT_SECRETS_FILE,
    TOKEN_FILES,
    YouTubeUploader,
    authorize_channel,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = PROJECT_ROOT / "workspace" / "raw"
TEMP_DIR = PROJECT_ROOT / "workspace" / "temp"
OUTPUT_EN_DIR = PROJECT_ROOT / "output" / "en"
OUTPUT_RU_DIR = PROJECT_ROOT / "output" / "ru"

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
WHISPER_AVAILABLE = importlib.util.find_spec("faster_whisper") is not None

EN_PINNED_COMMENT = (
    "Which moment was your favourite? Tell us below.\n"
    "More daily GTA VI clips: https://t.me/gta6clipfactory"
)
RU_PINNED_COMMENT = (
    "Больше клипов и эксклюзивов — в нашем Telegram: https://t.me/gta6clipfactory\n"
    "Подпишись, чтобы не пропустить новые Shorts!"
)
MANUAL_TITLE = "Manual clip"


def ensure_runtime_dirs() -> None:
    for directory in (DATA_DIR, RAW_DIR, TEMP_DIR, OUTPUT_EN_DIR, OUTPUT_RU_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    return load_base_config()


def _style_config() -> dict[str, Any]:
    config = load_config()
    overrides = {
        "font_size": st.session_state.get("font_size_control"),
        "primary_color": st.session_state.get("primary_color_control"),
        "outline_thickness": st.session_state.get("outline_control"),
        "margin_v": st.session_state.get("margin_v_control"),
    }
    for key, value in overrides.items():
        if value is not None:
            config[key] = value
    return config


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "clip_queue": [],
        "render_results": {},
        "layout_mode": "split",
        "font_size_control": load_config()["font_size"],
        "primary_color_control": load_config()["primary_color"],
        "outline_control": load_config()["outline_thickness"],
        "margin_v_control": load_config()["margin_v"],
        "schedule_interval": 24,
        "schedule_slot": 0,
        "schedule_date": (datetime.now(UTC) + timedelta(days=1)).date(),
        "schedule_time": time(18, 0),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clip_id_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    candidate = ""
    if "youtube" in parsed.netloc and "v" in parse_qs(parsed.query):
        candidate = parse_qs(parsed.query)["v"][0]
    else:
        candidate = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    candidate = candidate.split("#", 1)[0]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("._")
    return safe or "clip"


def find_streamer(clip_id: str) -> str:
    for clip in st.session_state.get("clip_queue", []):
        if str(clip.get("id")) == clip_id:
            return str(clip.get("streamer") or "")
    for clip in load_processed_clips():
        if str(clip.get("id")) == clip_id:
            return str(clip.get("streamer") or "")
    return ""


def _preset_for_clip(clip_id: str) -> str:
    presets = load_presets()
    streamer = find_streamer(clip_id)
    return streamer if streamer in presets else "default"


def _cam_box_for_clip(clip_id: str) -> tuple[float, float, float, float]:
    preset = load_presets().get(_preset_for_clip(clip_id), load_presets()["default"])
    return (
        float(st.session_state.get(f"cam_x_{clip_id}", preset["cam_x"])),
        float(st.session_state.get(f"cam_y_{clip_id}", preset["cam_y"])),
        float(st.session_state.get(f"cam_w_{clip_id}", preset["cam_w"])),
        float(st.session_state.get(f"cam_h_{clip_id}", preset["cam_h"])),
    )


def run_clip_pipeline(
    clip_id: str,
    raw_path: Path,
    layout_mode: str = "split",
    preset_name: str = "default",
    cam_box: tuple[float, float, float, float] | None = None,
    config: dict[str, Any] | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Transcribe, translate, then render EN and RU variants for one clip."""
    active_config = dict(config or load_config())
    processor = SubtitleProcessor(active_config)
    subtitle_data = processor.process(Path(raw_path), clip_id, progress=progress)
    hooks = {
        "en_hook": subtitle_data["en_hook"],
        "ru_hook": subtitle_data["ru_hook"],
    }
    renderer = FFmpegRenderer(
        preset_name=preset_name,
        cam_box=cam_box,
        config=active_config,
    )
    if progress:
        progress("Rendering EN version...")
    en_path = (
        renderer.render_blurred_fullscreen(
            clip_id, Path(raw_path), hooks, Path(subtitle_data["ass_path_en"]), lang="en"
        )
        if layout_mode == "blur"
        else renderer.render_split_screen(
            clip_id, Path(raw_path), hooks, Path(subtitle_data["ass_path_en"]), lang="en"
        )
    )
    if progress:
        progress("Rendering RU version with fingerprint filters...")
    ru_path = (
        renderer.render_blurred_fullscreen(
            clip_id, Path(raw_path), hooks, Path(subtitle_data["ass_path_ru"]), lang="ru"
        )
        if layout_mode == "blur"
        else renderer.render_split_screen(
            clip_id, Path(raw_path), hooks, Path(subtitle_data["ass_path_ru"]), lang="ru"
        )
    )
    result = dict(subtitle_data)
    result.update(
        {
            "en": en_path,
            "ru": ru_path,
            "hooks": hooks,
            "layout_mode": layout_mode,
            "preset": preset_name,
            "cam_box": cam_box or renderer.cam_box,
            "raw_path": str(raw_path),
        }
    )
    return result


def generate_synthetic_test_clip(
    config: dict[str, Any] | None = None,
    progress: Any = None,
    layout_mode: str | None = None,
    cam_box: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Create a five-second 1280x720 clip and run the complete pipeline."""
    ensure_runtime_dirs()
    if not HAS_FFMPEG:
        raise RuntimeError("ffmpeg and ffprobe must be installed before running the test.")
    raw_path = RAW_DIR / "synth_test.mp4"
    if progress:
        progress("Generating 5s testsrc + sine synthetic clip...")
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=5:size=1280x720:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=5",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(raw_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Synthetic clip generation failed: {(exc.stderr or '').strip()}") from exc
    active_config = dict(config or load_config())
    active_layout = layout_mode or st.session_state.get("layout_mode", "split")
    active_cam_box = cam_box if cam_box is not None else _cam_box_for_clip("synth_test")
    return run_clip_pipeline(
        "synth_test",
        raw_path,
        layout_mode=active_layout,
        preset_name="default",
        cam_box=active_cam_box,
        config=active_config,
        progress=progress,
    )


def open_output_folder(path: Path) -> bool:
    try:
        subprocess.Popen(
            ["xdg-open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, FileNotFoundError):
        return False


def _update_config_value(key: str, value: Any) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f'{key}: "{value}"' if isinstance(value, str) else f"{key}: {value}"
    if pattern.search(text):
        text = pattern.sub(replacement, text)
    else:
        text = text.rstrip() + "\n" + replacement + "\n"
    CONFIG_PATH.write_text(text, encoding="utf-8")


def _save_env_values(values: dict[str, str]) -> None:
    ENV_PATH.touch(exist_ok=True)
    for key, value in values.items():
        set_key(str(ENV_PATH), key, value or "", quote_mode="always")
        os.environ[key] = value or ""
    load_dotenv(ENV_PATH, override=True)


def _scheduled_start() -> datetime:
    selected_date: date = st.session_state.get(
        "schedule_date", (datetime.now(UTC) + timedelta(days=1)).date()
    )
    selected_time: time = st.session_state.get("schedule_time", time(18, 0))
    return datetime.combine(selected_date, selected_time, tzinfo=UTC)


def _format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "0:00"
    return f"{total // 60}:{total % 60:02d}"


def _load_translation_payload(clip_id: str) -> dict[str, Any]:
    path = TEMP_DIR / f"{clip_id}_translation.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_render_results() -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for clip_id, result in st.session_state.get("render_results", {}).items():
        results[clip_id] = dict(result)
    discovered: set[str] = set()
    for directory, suffix in ((OUTPUT_EN_DIR, "_en"), (OUTPUT_RU_DIR, "_ru")):
        for path in directory.glob("*.mp4"):
            if path.stem.endswith(suffix):
                discovered.add(path.stem[: -len(suffix)])
    for clip_id in discovered:
        if clip_id in results:
            continue
        payload = _load_translation_payload(clip_id)
        results[clip_id] = {
            "en": OUTPUT_EN_DIR / f"{clip_id}_en.mp4",
            "ru": OUTPUT_RU_DIR / f"{clip_id}_ru.mp4",
            "ass_path_en": TEMP_DIR / f"{clip_id}_en.ass",
            "ass_path_ru": TEMP_DIR / f"{clip_id}_ru.ass",
            "hooks": {
                "en_hook": payload.get("en_hook", "GTA VI MOMENT"),
                "ru_hook": payload.get("ru_hook", "МОМЕНТ ИЗ GTA VI"),
            },
            "layout_mode": "split",
            "preset": "default",
            "cam_box": (0.0, 0.0, 0.2, 0.25),
            "raw_path": str(RAW_DIR / f"{clip_id}.mp4"),
        }
    return results


@st.cache_data(show_spinner=False, max_entries=24)
def _cached_previews(
    path_str: str,
    _mtime: float,
    cam_box: tuple[float, float, float, float],
    layout: str,
    font_size: int,
    primary_color: str,
    outline: int,
    margin_v: int,
) -> tuple[Image.Image, Image.Image]:
    config = load_config()
    config.update(
        {
            "font_size": font_size,
            "primary_color": primary_color,
            "outline_thickness": outline,
            "margin_v": margin_v,
        }
    )
    renderer = FFmpegRenderer(cam_box=cam_box, config=config)
    frame_path = renderer.extract_middle_frame(Path(path_str))
    return (
        renderer.preview_source(frame_path, cam_box),
        renderer.preview_composite(frame_path, cam_box, layout),
    )


def render_sidebar_health() -> None:
    st.sidebar.title("GTA6 Clip Factory")
    st.sidebar.caption("Vertical Shorts / TikTok studio")
    st.sidebar.subheader("System health")
    if HAS_FFMPEG:
        st.sidebar.success("FFmpeg / ffprobe: OK")
    else:
        st.sidebar.error("FFmpeg / ffprobe: missing")
    model_name = load_config()["whisper_model"]
    if WHISPER_AVAILABLE:
        st.sidebar.success(f"Whisper ({model_name}, int8): ready")
    else:
        st.sidebar.error("faster-whisper: not installed")
    st.sidebar.metric("Processed clips", len(load_processed_clips()))
    st.sidebar.divider()
    st.sidebar.subheader("Zero-key verification")
    if st.sidebar.button("Generate Synthetic Test Clip", use_container_width=True):
        if not HAS_FFMPEG:
            st.sidebar.error("Install ffmpeg first.")
        else:
            with st.sidebar.status("Running synthetic pipeline...", expanded=True) as status:
                try:
                    result = generate_synthetic_test_clip(progress=status.write)
                    st.session_state.render_results["synth_test"] = result
                    status.update(label="Synthetic EN + RU renders complete", state="complete")
                except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
                    status.update(label="Synthetic pipeline failed", state="error")
                    st.sidebar.error(str(exc))


def tab_fetch_and_queue() -> None:
    st.header("Збір кліпів")
    manual_col, twitch_col = st.columns(2)
    with manual_col:
        st.subheader("Manual URLs")
        urls_text = st.text_area(
            "Twitch clip or YouTube URLs (one per line)",
            height=140,
            key="manual_urls",
        )
        if st.button("Add URLs to Queue", use_container_width=True):
            added = 0
            queue = list(st.session_state.clip_queue)
            known = {str(clip.get("id")) for clip in queue}
            for line in urls_text.splitlines():
                url = line.strip()
                if not url or not url.startswith(("http://", "https://")):
                    continue
                clip_id = clip_id_from_url(url)
                if clip_id in known:
                    continue
                queue.append(
                    {
                        "id": clip_id,
                        "url": url,
                        "title": MANUAL_TITLE,
                        "streamer": "manual",
                        "thumbnail": "",
                        "views": 0,
                        "duration": 0,
                        "source": "manual",
                    }
                )
                known.add(clip_id)
                added += 1
            st.session_state.clip_queue = queue
            if added:
                st.success(f"Added {added} URL(s) to the queue.")
            else:
                st.warning("No new valid URLs found.")

    with twitch_col:
        st.subheader("Twitch auto-fetcher")
        game_name = st.text_input("Game name", value="Grand Theft Auto VI", key="game_name")
        timeframe = st.selectbox("Timeframe", ["24h", "7d", "30d"], index=1, key="timeframe")
        limit = st.number_input("Clip limit", min_value=1, max_value=20, value=10, key="clip_limit")
        if st.button("Fetch Top Clips", use_container_width=True):
            if not (os.getenv("TWITCH_CLIENT_ID") and os.getenv("TWITCH_CLIENT_SECRET")):
                st.warning("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in Settings first.")
            else:
                with st.spinner("Querying Twitch Helix..."):
                    clips = fetch_twitch_clips(
                        game_name=game_name, first=int(limit), period=str(timeframe)
                    )
                queue = list(st.session_state.clip_queue)
                known = {str(clip.get("id")) for clip in queue}
                added = 0
                for clip in clips:
                    if str(clip.get("id")) in known:
                        continue
                    queue.append(clip)
                    known.add(str(clip.get("id")))
                    added += 1
                st.session_state.clip_queue = queue
                if added:
                    st.success(f"Added {added} Twitch clip(s).")
                else:
                    st.info("No new clips returned. Check credentials or the game name.")

    st.divider()
    st.subheader("Interactive clip queue")
    queue = list(st.session_state.get("clip_queue", []))
    if not queue:
        st.info("Add manual URLs or fetch Twitch clips to build the queue.")
        return

    processed_ids = {str(clip.get("id")) for clip in load_processed_clips()}
    selections: list[tuple[dict[str, Any], bool]] = []
    for index, clip in enumerate(queue):
        clip_id = str(clip.get("id") or f"clip_{index}")
        with st.container(border=True):
            cols = st.columns([1.2, 4, 1, 1, 1.1, 1.2])
            with cols[0]:
                thumbnail = str(clip.get("thumbnail") or "")
                if thumbnail.startswith(("http://", "https://")):
                    st.image(thumbnail, use_container_width=True)
                else:
                    st.caption("No thumbnail")
            with cols[1]:
                st.markdown(f"**{clip.get('title') or 'Untitled clip'}**")
                st.caption(
                    f"Streamer: {clip.get('streamer') or 'unknown'} | "
                    f"Source: {clip.get('source') or 'manual'}"
                )
                if clip_id in processed_ids:
                    st.success("Processed")
            with cols[2]:
                st.metric("Views", f"{int(clip.get('views') or 0):,}")
            with cols[3]:
                st.metric("Duration", _format_duration(clip.get("duration")))
            with cols[4]:
                selected = st.checkbox("Select", value=True, key=f"select_{clip_id}")
            with cols[5]:
                force = st.checkbox("Force re-download", key=f"force_{clip_id}")
            selections.append((clip, selected and (force or clip_id not in processed_ids)))

    if st.button("Download Selected to Workspace", type="primary", use_container_width=True):
        targets = [clip for clip, selected in selections if selected]
        if not targets:
            st.warning("Select at least one clip that is not already downloaded.")
        else:
            fetcher = ClipFetcher()
            progress = st.progress(0.0, text="Downloading clips...")
            downloaded = 0
            for index, clip in enumerate(targets):
                force = bool(st.session_state.get(f"force_{clip.get('id')}", False))
                result = fetcher.process_clip(clip, RAW_DIR, force=force)
                clip_id = str(clip.get("id"))
                if result["ok"]:
                    path = result["path"] or RAW_DIR / f"{clip_id}.mp4"
                    st.session_state.render_results.setdefault(clip_id, {})["raw_path"] = str(path)
                    downloaded += 1
                else:
                    st.error(f"{clip_id}: {result['error']}")
                progress.progress(
                    (index + 1) / len(targets),
                    text=f"Downloaded {downloaded}/{len(targets)}",
                )
            if downloaded:
                st.success(f"Downloaded {downloaded} clip(s) into workspace/raw.")


def tab_crop_style_studio() -> None:
    st.header("Рамка вебкамери та стиль")
    raw_files = sorted(RAW_DIR.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not raw_files:
        st.info("Download clips in the first tab or generate the synthetic test clip.")
        return

    labels = [path.name for path in raw_files]
    selected_label = st.selectbox("Clip from workspace/raw", labels, key="studio_clip")
    clip_id = Path(selected_label).stem
    presets = load_presets()
    streamer = find_streamer(clip_id)
    default_preset = streamer if streamer in presets else "default"
    preset_names = sorted(presets)
    preset_name = st.selectbox(
        "Streamer preset",
        preset_names,
        index=preset_names.index(default_preset),
        key="studio_preset",
    )

    signature = f"{clip_id}|{preset_name}"
    if st.session_state.get("preview_signature") != signature:
        preset = presets[preset_name]
        for axis in ("cam_x", "cam_y", "cam_w", "cam_h"):
            st.session_state[f"{axis}_{clip_id}"] = float(preset[axis])
        st.session_state["preview_signature"] = signature

    st.subheader("Webcam crop (normalized 0.0 – 1.0)")
    x_col, y_col, w_col, h_col = st.columns(4)
    with x_col:
        cam_x = st.slider("cam_x", 0.0, 1.0, step=0.01, key=f"cam_x_{clip_id}")
    with y_col:
        cam_y = st.slider("cam_y", 0.0, 1.0, step=0.01, key=f"cam_y_{clip_id}")
    with w_col:
        cam_w = st.slider("cam_w", 0.01, 1.0, step=0.01, key=f"cam_w_{clip_id}")
    with h_col:
        cam_h = st.slider("cam_h", 0.01, 1.0, step=0.01, key=f"cam_h_{clip_id}")
    if cam_x + cam_w > 1.0 or cam_y + cam_h > 1.0:
        st.warning("The webcam crop extends past the source frame; FFmpeg will clamp it.")

    layout_mode = st.radio(
        "Layout mode",
        ["split", "blur"],
        format_func=lambda value: (
            "Split-Screen (Webcam + Gameplay)" if value == "split" else "Full-Screen 9:16 with Blurred BG"
        ),
        horizontal=True,
        key="layout_mode",
    )

    with st.expander("Subtitle & hook styling", expanded=True):
        style_cols = st.columns(4)
        with style_cols[0]:
            st.slider("Font size", 24, 72, key="font_size_control")
        with style_cols[1]:
            st.color_picker("Primary color", key="primary_color_control")
        with style_cols[2]:
            st.slider("Outline thickness", 0, 6, key="outline_control")
        with style_cols[3]:
            st.slider("Bottom margin (MarginV)", 100, 400, key="margin_v_control")

    config = _style_config()
    cam_box = (cam_x, cam_y, cam_w, cam_h)
    source_path = RAW_DIR / selected_label
    st.subheader("Live visual preview")
    try:
        source_image, composite_image = _cached_previews(
            str(source_path),
            source_path.stat().st_mtime,
            cam_box,
            layout_mode,
            int(config["font_size"]),
            str(config["primary_color"]),
            int(config["outline_thickness"]),
            int(config["margin_v"]),
        )
        left, right = st.columns(2)
        with left:
            st.image(
                source_image,
                caption="Source: green = webcam crop, blue = gameplay",
                use_container_width=True,
            )
        with right:
            st.image(
                composite_image,
                caption="Resulting 9:16 composite (sample hook + subtitle)",
                use_container_width=True,
            )
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        st.error(f"Preview failed: {exc}")

    st.divider()
    save_cols = st.columns([3, 1])
    with save_cols[0]:
        preset_streamer = st.text_input(
            "Save this crop for streamer", value=streamer or "default", key="preset_streamer"
        )
    with save_cols[1]:
        if st.button("Save Preset for Streamer", use_container_width=True):
            save_preset(preset_streamer, cam_x, cam_y, cam_w, cam_h)
            st.success(f"Preset '{preset_streamer}' saved to presets.json.")


def _default_title(clip_id: str, language: str, hook: str) -> str:
    headline = hook.strip() or ("GTA VI момент" if language == "ru" else "GTA VI moment")
    if language == "ru":
        return f"{headline} | GTA VI #Shorts"[:100]
    return f"GTA VI: {headline} #Shorts"[:100]


def _default_description(language: str, hook: str) -> str:
    if language == "ru":
        return (
            f"{hook}\n\n"
            "Клип из GTA VI в вертикальном формате. Подпишись, чтобы не пропустить новые Shorts!\n"
            "#GTA6 #Shorts #Игры #Gaming"
        )
    return (
        f"{hook}\n\n"
        "Vertical GTA VI clip. Subscribe for daily Shorts and viral moments!\n"
        "#GTA6 #Shorts #Gaming #Viral"
    )


def _render_language_panel(clip_id: str, language: str, result: dict[str, Any]) -> None:
    hook_key = "en_hook" if language == "en" else "ru_hook"
    hooks = dict(result.get("hooks") or {})
    default_hook = str(hooks.get(hook_key) or "")
    video_path = result.get(language)
    if video_path and Path(video_path).exists():
        st.video(str(video_path))
    else:
        st.info(f"{language.upper()} render is not available yet.")

    hook_value = st.text_input(
        "Hook Banner Text",
        value=default_hook,
        key=f"hook_{clip_id}_{language}",
    )
    title_value = st.text_input(
        "Video Title",
        value=_default_title(clip_id, language, default_hook),
        key=f"title_{clip_id}_{language}",
    )
    description_value = st.text_area(
        "Description & Hashtags",
        value=_default_description(language, default_hook),
        height=130,
        key=f"description_{clip_id}_{language}",
    )
    comment_value = st.text_area(
        "Pinned Comment CTA",
        value=EN_PINNED_COMMENT if language == "en" else RU_PINNED_COMMENT,
        height=90,
        key=f"comment_{clip_id}_{language}",
    )

    action_cols = st.columns(3)
    if action_cols[0].button("Re-render Hook Text", key=f"rerender_{clip_id}_{language}"):
        try:
            config = _style_config()
            renderer = FFmpegRenderer(
                preset_name=str(result.get("preset", "default")),
                cam_box=result.get("cam_box"),
                config=config,
            )
            updated_hooks = dict(hooks)
            updated_hooks[hook_key] = hook_value
            ass_path = Path(
                result.get(f"ass_path_{language}") or TEMP_DIR / f"{clip_id}_{language}.ass"
            )
            layout = str(result.get("layout_mode", "split"))
            render_call = (
                renderer.render_blurred_fullscreen
                if layout == "blur"
                else renderer.render_split_screen
            )
            new_path = render_call(
                clip_id,
                Path(result.get("raw_path") or RAW_DIR / f"{clip_id}.mp4"),
                updated_hooks,
                ass_path,
                lang=language,
            )
            stored = st.session_state.render_results.setdefault(clip_id, dict(result))
            stored[language] = new_path
            stored["hooks"] = updated_hooks
            st.success(f"{language.upper()} hook re-rendered.")
        except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
            st.error(f"Re-render failed: {exc}")

    if action_cols[1].button("Open Output Folder", key=f"open_{clip_id}_{language}"):
        folder = OUTPUT_EN_DIR if language == "en" else OUTPUT_RU_DIR
        if open_output_folder(folder):
            st.success(f"Opened {folder}")
        else:
            st.error("Could not launch xdg-open. Folder: " + str(folder))

    if action_cols[2].button("Schedule to YouTube", key=f"schedule_{clip_id}_{language}"):
        if not video_path or not Path(video_path).exists():
            st.error("Render the video before scheduling it.")
        else:
            uploader = YouTubeUploader()
            ok = uploader.upload(
                Path(video_path),
                title_value,
                description_value + "\n\n" + comment_value,
                language=language,
                start_time=_scheduled_start(),
                interval_hours=int(st.session_state.get("schedule_interval", 24)),
                slot_index=int(st.session_state.get("schedule_slot", 0)),
            )
            if ok:
                st.success("Upload scheduled on YouTube.")
            else:
                st.error("YouTube scheduling failed. Check the OAuth token and daily quota.")


def render_clip_gallery(clip_id: str, result: dict[str, Any]) -> None:
    with st.expander(f"Clip {clip_id}", expanded=True):
        en_col, ru_col = st.columns(2)
        with en_col:
            st.markdown("### English Version (EN)")
            _render_language_panel(clip_id, "en", result)
        with ru_col:
            st.markdown("### Russian Version (RU)")
            _render_language_panel(clip_id, "ru", result)


def tab_batch_render() -> None:
    st.header("Рендер та перевірка")
    raw_files = sorted(RAW_DIR.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not raw_files:
        st.info("No clips in workspace/raw. Download or generate a test clip first.")
        return

    labels = [path.name for path in raw_files]
    selected = st.multiselect(
        "Clips to render (EN + RU)",
        labels,
        default=labels,
        key="batch_selection",
    )
    layout_mode = st.session_state.get("layout_mode", "split")
    st.caption(
        "Layout: "
        + ("Split-Screen" if layout_mode == "split" else "Full-Screen blurred")
        + " | RU: 1.04x zoom, colour grade, 1.02x speed, resynced subtitles"
    )

    if st.button("Render Selected Clips (EN + RU)", type="primary", use_container_width=True):
        if not selected:
            st.warning("Select at least one clip to render.")
        else:
            overall = st.progress(0.0, text="Starting batch render...")
            completed = 0
            for index, label in enumerate(selected):
                clip_id = Path(label).stem
                raw_path = RAW_DIR / label
                with st.status(f"Processing {clip_id}", expanded=True) as status:
                    try:
                        result = run_clip_pipeline(
                            clip_id,
                            raw_path,
                            layout_mode=layout_mode,
                            preset_name=_preset_for_clip(clip_id),
                            cam_box=_cam_box_for_clip(clip_id),
                            config=_style_config(),
                            progress=status.write,
                        )
                        st.session_state.render_results[clip_id] = result
                        status.update(label=f"Rendered {clip_id}", state="complete")
                        completed += 1
                    except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
                        status.update(label=f"Failed {clip_id}", state="error")
                        st.error(f"{clip_id}: {exc}")
                overall.progress(
                    (index + 1) / len(selected),
                    text=f"Rendered {completed}/{len(selected)}",
                )
            if completed:
                st.success(f"Batch complete: {completed} clip(s) rendered.")

    st.divider()
    st.subheader("Side-by-Side Output Gallery")
    results = collect_render_results()
    if not results:
        st.info("Rendered outputs will appear here with editable publishing metadata.")
        return
    for clip_id in sorted(results):
        render_clip_gallery(clip_id, results[clip_id])


def tab_settings_channels() -> None:
    st.header("Налаштування та API")
    credentials_col, model_col = st.columns(2)
    with credentials_col:
        st.subheader("Credentials (.env)")
        twitch_id = st.text_input(
            "TWITCH_CLIENT_ID", value=os.getenv("TWITCH_CLIENT_ID", ""), key="env_twitch_id"
        )
        twitch_secret = st.text_input(
            "TWITCH_CLIENT_SECRET",
            value=os.getenv("TWITCH_CLIENT_SECRET", ""),
            type="password",
            key="env_twitch_secret",
        )
        llm_key = st.text_input(
            "LLM_API_KEY",
            value=os.getenv("LLM_API_KEY", ""),
            type="password",
            key="env_llm_key",
        )
        llm_url = st.text_input(
            "LLM_BASE_URL",
            value=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            key="env_llm_url",
        )
        llm_model = st.text_input(
            "LLM_MODEL", value=os.getenv("LLM_MODEL", "llama-3.1-8b-instant"), key="env_llm_model"
        )
        if st.button("Save .env", use_container_width=True):
            _save_env_values(
                {
                    "TWITCH_CLIENT_ID": twitch_id,
                    "TWITCH_CLIENT_SECRET": twitch_secret,
                    "LLM_API_KEY": llm_key,
                    "LLM_BASE_URL": llm_url,
                    "LLM_MODEL": llm_model,
                }
            )
            st.success(".env saved. New values are active immediately.")

    with model_col:
        st.subheader("Whisper model")
        config = load_config()
        model_options = ["tiny", "base", "small", "medium"]
        current_model = str(config.get("whisper_model", "small"))
        if current_model not in model_options:
            model_options.append(current_model)
        selected_model = st.selectbox(
            "CPU transcription model",
            model_options,
            index=model_options.index(current_model),
            key="settings_whisper_model",
        )
        st.caption("int8 compute type is used for CPU-first execution.")
        if st.button("Save Whisper Model", use_container_width=True):
            _update_config_value("whisper_model", selected_model)
            st.success(f"Whisper model set to '{selected_model}'.")

    st.divider()
    st.subheader("YouTube channels")
    uploader = YouTubeUploader()
    statuses = uploader.token_status()
    channel_columns = st.columns(2)
    for column, channel in zip(channel_columns, ("en", "ru"), strict=False):
        with column:
            st.markdown(f"#### {channel.upper()} channel")
            info = statuses[channel]
            if info["exists"]:
                if info["valid"]:
                    st.success("OAuth token connected and valid.")
                elif info["has_refresh_token"]:
                    st.info("OAuth token present and refreshable.")
                else:
                    st.warning("OAuth token present but expired without a refresh token.")
            else:
                st.warning("No OAuth token connected.")
            st.caption(f"Token file: {TOKEN_FILES[channel]}")
            if st.button(f"Connect {channel.upper()} channel", key=f"connect_{channel}"):
                if not CLIENT_SECRETS_FILE.exists():
                    st.error(
                        "Missing data/client_secret.json. Download an OAuth client from "
                        "Google Cloud Console and save it there first."
                    )
                else:
                    try:
                        token_path = authorize_channel(channel)
                        st.success(f"Connected. Token saved to {token_path}.")
                    except Exception as exc:  # noqa: BLE001 - surface OAuth UI failures
                        st.error(f"OAuth connection failed: {exc}")

    quotas = uploader.check_quota_status()
    st.subheader("Scheduling controls")
    schedule_cols = st.columns(4)
    with schedule_cols[0]:
        st.date_input("Start date (UTC)", key="schedule_date")
    with schedule_cols[1]:
        st.time_input("Start time (UTC)", key="schedule_time")
    with schedule_cols[2]:
        st.number_input("Spacing (hours)", min_value=1, max_value=168, key="schedule_interval")
    with schedule_cols[3]:
        st.number_input("Slot index", min_value=0, max_value=4, key="schedule_slot")
    st.caption(
        f"Hard guard: maximum 5 uploads per channel per UTC day. "
        f"EN used today: {quotas['en']['used_today']}/{quotas['en']['max_daily']}; "
        f"RU used today: {quotas['ru']['used_today']}/{quotas['ru']['max_daily']}."
    )
    st.dataframe(
        [
            {
                "Channel": channel.upper(),
                "Connected": quotas[channel]["connected"],
                "Max/day": quotas[channel]["max_daily"],
                "Used today": quotas[channel]["used_today"],
                "Remaining": quotas[channel]["remaining"],
                "OK": quotas[channel]["ok"],
            }
            for channel in ("en", "ru")
        ],
        use_container_width=True,
        hide_index=True,
    )
    token_refresh = st.button("Refresh OAuth status", use_container_width=True)
    if token_refresh:
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="GTA6 Clip Factory",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    ensure_runtime_dirs()
    _init_state()
    render_sidebar_health()
    fetch_tab, crop_tab, render_tab, settings_tab = st.tabs(
        [
            "Збір кліпів",
            "Рамка вебкамери та стиль",
            "Рендер та перевірка",
            "Налаштування та API",
        ]
    )
    with fetch_tab:
        tab_fetch_and_queue()
    with crop_tab:
        tab_crop_style_studio()
    with render_tab:
        tab_batch_render()
    with settings_tab:
        tab_settings_channels()


load_dotenv(ENV_PATH)

if __name__ == "__main__":
    main()
