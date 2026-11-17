"""ClipForge Studio — open-source multi-game, multi-language clip studio.

Run with ``streamlit run app.py``.  Importing this module has no Streamlit side
effects, so the synthetic smoke test can reuse the pipeline helpers directly.
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
from src.languages import (
    language_badge,
    language_name,
    normalize_language,
    supported_languages_from_config,
)
from src.renderer import FFmpegRenderer, load_presets, save_preset
from src.transcriber import SubtitleProcessor, build_template_metadata
from src.transcriber import load_config as load_base_config
from src.uploader import (
    CLIENT_SECRETS_FILE,
    YouTubeUploader,
    authorize_channel,
    get_token_path,
    list_connected_languages,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = PROJECT_ROOT / "workspace" / "raw"
TEMP_DIR = PROJECT_ROOT / "workspace" / "temp"
OUTPUT_ROOT = PROJECT_ROOT / "output"

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
WHISPER_AVAILABLE = importlib.util.find_spec("faster_whisper") is not None

ALL_LANGUAGES = "__all__"
CUSTOM_LANGUAGE = "__custom__"
GAME_PRESETS = [
    "Grand Theft Auto VI",
    "Counter-Strike 2",
    "Valorant",
    "Dota 2",
    "Minecraft",
    "Just Chatting",
]
MANUAL_TITLE = "Manual clip"


def ensure_runtime_dirs() -> None:
    for directory in (DATA_DIR, RAW_DIR, TEMP_DIR, OUTPUT_ROOT):
        directory.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    return load_base_config()


def _supported_languages() -> list[str]:
    return supported_languages_from_config(load_config())


def _language_label(code: str) -> str:
    return f"{language_name(code)} ({code.upper()})"


def _game_preset_options() -> list[str]:
    default_game = str(load_config().get("default_game") or GAME_PRESETS[0]).strip()
    options: list[str] = []
    for name in [default_game, *GAME_PRESETS]:
        if name and name not in options:
            options.append(name)
    return options


def _apply_game_preset() -> None:
    st.session_state.game_name_control = st.session_state.game_preset_control


def _active_pipeline_language() -> str:
    config = load_config()
    default = normalize_language(config.get("default_language")) or "en"
    choice = st.session_state.get("fetch_language_choice")
    if choice == CUSTOM_LANGUAGE:
        return normalize_language(st.session_state.get("custom_language_code")) or default
    if not choice or choice == ALL_LANGUAGES:
        return default
    return normalize_language(choice) or default


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
    config = load_config()
    preset_options = _game_preset_options()
    default_game = str(config.get("default_game") or preset_options[0]).strip()
    defaults: dict[str, Any] = {
        "clip_queue": [],
        "render_results": {},
        "layout_mode": "split",
        "font_size_control": config["font_size"],
        "primary_color_control": config["primary_color"],
        "outline_control": config["outline_thickness"],
        "margin_v_control": config["margin_v"],
        "schedule_interval": 24,
        "schedule_slot": 0,
        "schedule_date": (datetime.now(UTC) + timedelta(days=1)).date(),
        "schedule_time": time(18, 0),
        "game_name_control": default_game,
        "game_preset_control": default_game if default_game in preset_options else preset_options[0],
        "pipeline_language": normalize_language(config.get("default_language")) or "en",
        "render_uniqueness": bool(config.get("anti_duplicate_enabled", True)),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clip_id_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
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


def find_clip_language(clip_id: str) -> str:
    for clip in st.session_state.get("clip_queue", []):
        if str(clip.get("id")) == clip_id:
            return normalize_language(clip.get("language")) or ""
    for clip in load_processed_clips():
        if str(clip.get("id")) == clip_id:
            return normalize_language(clip.get("language")) or ""
    return ""


def _preset_for_clip(clip_id: str) -> str:
    presets = load_presets()
    streamer = find_streamer(clip_id)
    return streamer if streamer in presets else "default"


def _cam_box_for_clip(clip_id: str) -> tuple[float, float, float, float]:
    presets = load_presets()
    preset = presets.get(_preset_for_clip(clip_id), presets["default"])
    return (
        float(st.session_state.get(f"cam_x_{clip_id}", preset["cam_x"])),
        float(st.session_state.get(f"cam_y_{clip_id}", preset["cam_y"])),
        float(st.session_state.get(f"cam_w_{clip_id}", preset["cam_w"])),
        float(st.session_state.get(f"cam_h_{clip_id}", preset["cam_h"])),
    )


def run_clip_pipeline(
    clip_id: str,
    raw_path: Path,
    source_lang: str | None = None,
    target_lang: str | None = None,
    game_name: str = "",
    layout_mode: str = "split",
    preset_name: str = "default",
    cam_box: tuple[float, float, float, float] | None = None,
    config: dict[str, Any] | None = None,
    progress: Any = None,
    apply_uniqueness: bool = True,
) -> dict[str, Any]:
    """Transcribe, localise and render every requested language variant."""
    active_config = dict(config or load_config())
    default_language = normalize_language(active_config.get("default_language")) or "en"
    source = normalize_language(source_lang) or default_language
    game = str(game_name or active_config.get("default_game") or "").strip()
    processor = SubtitleProcessor(active_config)
    subtitle_data = processor.process(
        Path(raw_path),
        clip_id,
        source_lang=source,
        target_lang=target_lang,
        game_name=game,
        progress=progress,
    )
    renderer = FFmpegRenderer(
        preset_name=preset_name,
        cam_box=cam_box,
        config=active_config,
    )
    outputs: dict[str, Path] = {}
    hooks: dict[str, str] = {}
    for lang_code, variant in subtitle_data.get("variants", {}).items():
        if progress:
            progress(f"Rendering {lang_code.upper()} version...")
        metadata = dict(variant.get("metadata") or {})
        output_path = renderer.render_clip(
            clip_id,
            Path(raw_path),
            str(metadata.get("hook") or ""),
            Path(variant["ass_path"]),
            lang_code,
            layout=layout_mode,
            apply_uniqueness=apply_uniqueness,
        )
        outputs[lang_code] = output_path
        hooks[lang_code] = str(metadata.get("hook") or "")
        variant["output_path"] = str(output_path)
    result = dict(subtitle_data)
    result.update(
        {
            "outputs": outputs,
            "hooks": hooks,
            "layout_mode": layout_mode,
            "preset": preset_name,
            "cam_box": cam_box or renderer.cam_box,
            "raw_path": str(raw_path),
            "game_name": game,
        }
    )
    return result


def generate_synthetic_test_clip(
    config: dict[str, Any] | None = None,
    progress: Any = None,
    layout_mode: str | None = None,
    cam_box: tuple[float, float, float, float] | None = None,
    language: str | None = None,
    target_lang: str | None = None,
    game_name: str | None = None,
    clip_id: str = "synth_test",
) -> dict[str, Any]:
    """Create a five-second test clip and run the selected-language pipeline."""
    ensure_runtime_dirs()
    if not HAS_FFMPEG:
        raise RuntimeError("ffmpeg and ffprobe must be installed before running the test.")
    active_config = dict(config or load_config())
    active_language = normalize_language(language) or normalize_language(
        active_config.get("default_language")
    ) or "en"
    raw_path = RAW_DIR / f"{clip_id}.mp4"
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
    active_layout = layout_mode or st.session_state.get("layout_mode", "split")
    active_cam_box = cam_box if cam_box is not None else _cam_box_for_clip(clip_id)
    return run_clip_pipeline(
        clip_id,
        raw_path,
        source_lang=active_language,
        target_lang=target_lang,
        game_name=game_name or str(active_config.get("default_game") or ""),
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


def _load_localization_payload(clip_id: str) -> dict[str, Any]:
    path = TEMP_DIR / f"{clip_id}_localization.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_render_results() -> dict[str, dict[str, Any]]:
    """Merge session results with outputs discovered under output/<lang>/."""
    results: dict[str, dict[str, Any]] = {
        clip_id: dict(result)
        for clip_id, result in st.session_state.get("render_results", {}).items()
    }
    discovered: dict[str, set[str]] = {}
    for path in OUTPUT_ROOT.glob("*/*.mp4"):
        lang = normalize_language(path.parent.name)
        if not lang:
            continue
        suffix = f"_{lang}"
        if not path.stem.endswith(suffix):
            continue
        clip_id = path.stem[: -len(suffix)]
        discovered.setdefault(clip_id, set()).add(lang)

    default_game = str(load_config().get("default_game") or "")
    for clip_id, languages in discovered.items():
        existing = results.get(clip_id, {})
        payload = _load_localization_payload(clip_id)
        variants = dict(existing.get("variants") or payload.get("variants") or {})
        for lang in languages:
            variant = dict(variants.get(lang) or {})
            variant.setdefault("lang", lang)
            variant.setdefault("ass_path", str(TEMP_DIR / f"{clip_id}_{lang}.ass"))
            variant.setdefault(
                "metadata",
                build_template_metadata(
                    lang, payload.get("game_name") or default_game, ""
                ),
            )
            variant["output_path"] = str(OUTPUT_ROOT / lang / f"{clip_id}_{lang}.mp4")
            variants[lang] = variant
        merged = dict(payload)
        merged.update(existing)
        merged["variants"] = variants
        merged.setdefault("clip_id", clip_id)
        merged.setdefault("game_name", payload.get("game_name") or default_game)
        merged.setdefault("raw_path", str(RAW_DIR / f"{clip_id}.mp4"))
        merged.setdefault("layout_mode", "split")
        results[clip_id] = merged
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
    hook_text: str,
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
        renderer.preview_composite(frame_path, cam_box, layout, hook_text),
    )


def render_sidebar_health() -> None:
    st.sidebar.title("ClipForge Studio")
    st.sidebar.caption("Open-source multi-game vertical clip studio")
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
    active_language = _active_pipeline_language()
    st.sidebar.caption(
        f"Pipeline language: {language_badge(active_language)} {language_name(active_language)}"
    )
    st.sidebar.divider()
    st.sidebar.subheader("Zero-key verification")
    if st.sidebar.button("Generate Synthetic Test Clip", width="stretch"):
        if not HAS_FFMPEG:
            st.sidebar.error("Install ffmpeg first.")
        else:
            with st.sidebar.status("Running synthetic pipeline...", expanded=True) as status:
                try:
                    result = generate_synthetic_test_clip(
                        progress=status.write, language=active_language
                    )
                    st.session_state.render_results["synth_test"] = result
                    status.update(
                        label=f"Synthetic {active_language.upper()} render complete",
                        state="complete",
                    )
                except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
                    status.update(label="Synthetic pipeline failed", state="error")
                    st.sidebar.error(str(exc))


def tab_fetch_and_queue() -> None:
    st.header("Fetch & Queue")
    st.caption("Step 1 chooses the language, Step 2 chooses the game, then fetch or paste URLs.")
    language_col, game_col = st.columns(2)
    with language_col:
        st.subheader("Step 1 · Language")
        options = [ALL_LANGUAGES, *_supported_languages(), CUSTOM_LANGUAGE]

        def _format_language(value: str) -> str:
            if value == ALL_LANGUAGES:
                return "All languages"
            if value == CUSTOM_LANGUAGE:
                return "Custom ISO code…"
            return _language_label(value)

        choice = st.selectbox(
            "Clip language",
            options,
            key="fetch_language_choice",
            format_func=_format_language,
        )
        custom_code = ""
        if choice == CUSTOM_LANGUAGE:
            custom_code = st.text_input(
                "Custom ISO 639-1 code", value="en", key="custom_language_code"
            )
        selected_language = None
        if choice == CUSTOM_LANGUAGE:
            selected_language = normalize_language(custom_code)
            if not selected_language:
                st.warning("Enter a valid 2–3 letter language code, for example 'uk'.")
        elif choice != ALL_LANGUAGES:
            selected_language = normalize_language(choice)
        st.session_state.pipeline_language = (
            selected_language or _active_pipeline_language()
        )

    with game_col:
        st.subheader("Step 2 · Game")
        preset_options = _game_preset_options()
        if st.session_state.get("game_preset_control") not in preset_options:
            st.session_state.game_preset_control = preset_options[0]
        st.selectbox(
            "Quick-select game",
            preset_options,
            key="game_preset_control",
            on_change=_apply_game_preset,
        )
        game_name = st.text_input("Game name", key="game_name_control")

    manual_col, twitch_col = st.columns(2)
    with manual_col:
        st.subheader("Manual URLs")
        urls_text = st.text_area(
            "Twitch clip or YouTube URLs (one per line)",
            height=140,
            key="manual_urls",
        )
        if st.button("Add URLs to Queue", width="stretch"):
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
                        "language": selected_language or "",
                        "game": game_name,
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
        timeframe = st.selectbox("Timeframe", ["24h", "7d", "30d"], index=1, key="timeframe")
        limit = st.number_input(
            "Clip limit", min_value=1, max_value=50, value=10, key="clip_limit"
        )
        if st.button("Fetch Top Clips", width="stretch"):
            if not (os.getenv("TWITCH_CLIENT_ID") and os.getenv("TWITCH_CLIENT_SECRET")):
                st.warning("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in Settings first.")
            else:
                with st.spinner("Querying Twitch Helix and filtering by language..."):
                    clips = fetch_twitch_clips(
                        game_name=game_name,
                        first=int(limit),
                        period=str(timeframe),
                        language=selected_language,
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
                    if selected_language and added < int(limit):
                        st.info(
                            f"Twitch only returned {added} clips in "
                            f"{language_badge(selected_language)} after scanning up to "
                            "1000 candidates."
                        )
                else:
                    st.info("No new clips returned. Check credentials, game name or language.")

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
                    st.image(thumbnail, width="stretch")
                else:
                    st.caption("No thumbnail")
            with cols[1]:
                st.markdown(
                    f"**{clip.get('title') or 'Untitled clip'}** "
                    f"{language_badge(clip.get('language'))}"
                )
                st.caption(
                    f"Game: {clip.get('game') or game_name or 'unknown'} | "
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

    if st.button("Download Selected to Workspace", type="primary", width="stretch"):
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
    st.header("Crop & Style")
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
            "Split-Screen (Webcam + Gameplay)"
            if value == "split"
            else "Full-Screen 9:16 with Blurred BG"
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
    preview_language = _active_pipeline_language()
    preview_hook = build_template_metadata(
        preview_language,
        str(config.get("default_game") or ""),
        "",
    )["hook"]
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
            preview_hook,
        )
        left, right = st.columns(2)
        with left:
            st.image(
                source_image,
                caption="Source: green = webcam crop, blue = gameplay",
                width="stretch",
            )
        with right:
            st.image(
                composite_image,
                caption="Resulting 9:16 composite (sample hook + subtitle)",
                width="stretch",
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
        if st.button("Save Preset for Streamer", width="stretch"):
            save_preset(preset_streamer, cam_x, cam_y, cam_w, cam_h)
            st.success(f"Preset '{preset_streamer}' saved to presets.json.")


def _render_variant_panel(
    clip_id: str,
    lang_code: str,
    variant: dict[str, Any],
    result: dict[str, Any],
) -> None:
    metadata = dict(variant.get("metadata") or {})
    fallback = build_template_metadata(
        lang_code, str(result.get("game_name") or ""), ""
    )
    for key, value in fallback.items():
        metadata.setdefault(key, value)

    st.markdown(f"#### {language_name(lang_code)} {language_badge(lang_code)}")
    video_path = variant.get("output_path") or (
        result.get("outputs", {}) or {}
    ).get(lang_code)
    if video_path and Path(video_path).exists():
        st.video(str(video_path))
    else:
        st.info(f"{lang_code.upper()} render is not available yet.")

    hook_value = st.text_input(
        "Hook Banner Text",
        value=str(metadata.get("hook") or ""),
        key=f"hook_{clip_id}_{lang_code}",
    )
    title_value = st.text_input(
        "Video Title",
        value=str(metadata.get("title") or ""),
        key=f"title_{clip_id}_{lang_code}",
    )
    description_value = st.text_area(
        "Description & Hashtags",
        value=str(metadata.get("description") or ""),
        height=130,
        key=f"description_{clip_id}_{lang_code}",
    )
    comment_value = st.text_area(
        "Pinned Comment CTA",
        value=str(metadata.get("cta") or ""),
        height=90,
        key=f"comment_{clip_id}_{lang_code}",
    )

    action_cols = st.columns(3)
    if action_cols[0].button("Re-render Hook Text", key=f"rerender_{clip_id}_{lang_code}"):
        try:
            config = _style_config()
            renderer = FFmpegRenderer(
                preset_name=str(result.get("preset", "default")),
                cam_box=result.get("cam_box"),
                config=config,
            )
            ass_path = Path(
                variant.get("ass_path") or TEMP_DIR / f"{clip_id}_{lang_code}.ass"
            )
            new_path = renderer.render_clip(
                clip_id,
                Path(result.get("raw_path") or RAW_DIR / f"{clip_id}.mp4"),
                hook_value,
                ass_path,
                lang_code,
                layout=str(result.get("layout_mode", "split")),
                apply_uniqueness=bool(st.session_state.get("render_uniqueness", True)),
            )
            updated_metadata = {
                **metadata,
                "hook": hook_value,
                "title": title_value,
                "description": description_value,
                "cta": comment_value,
            }
            stored = st.session_state.render_results.setdefault(clip_id, dict(result))
            variants = dict(stored.get("variants") or result.get("variants") or {})
            variant_update = dict(variants.get(lang_code) or variant)
            variant_update["output_path"] = str(new_path)
            variant_update["metadata"] = updated_metadata
            variants[lang_code] = variant_update
            stored["variants"] = variants
            outputs = dict(stored.get("outputs") or {})
            outputs[lang_code] = new_path
            stored["outputs"] = outputs
            st.success(f"{lang_code.upper()} hook re-rendered.")
        except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
            st.error(f"Re-render failed: {exc}")

    if action_cols[1].button("Open Output Folder", key=f"open_{clip_id}_{lang_code}"):
        folder = OUTPUT_ROOT / lang_code
        if open_output_folder(folder):
            st.success(f"Opened {folder}")
        else:
            st.error("Could not launch xdg-open. Folder: " + str(folder))

    if action_cols[2].button("Schedule to YouTube", key=f"schedule_{clip_id}_{lang_code}"):
        if not video_path or not Path(video_path).exists():
            st.error("Render the video before scheduling it.")
        else:
            uploader = YouTubeUploader()
            ok = uploader.upload(
                Path(video_path),
                title_value,
                description_value + "\n\n" + comment_value,
                language=lang_code,
                start_time=_scheduled_start(),
                interval_hours=int(st.session_state.get("schedule_interval", 24)),
                slot_index=int(st.session_state.get("schedule_slot", 0)),
            )
            if ok:
                st.success("Upload scheduled on YouTube.")
            else:
                st.error("YouTube scheduling failed. Check the OAuth token and daily quota.")


def render_clip_gallery(clip_id: str, result: dict[str, Any]) -> None:
    game = str(result.get("game_name") or "")
    with st.expander(f"Clip {clip_id}" + (f" · {game}" if game else ""), expanded=True):
        variants = result.get("variants") or {}
        if not variants:
            st.info("No language variants are available for this clip yet.")
            return
        languages = list(variants)
        for start in range(0, len(languages), 2):
            columns = st.columns(2)
            for column, lang_code in zip(columns, languages[start : start + 2], strict=False):
                with column:
                    _render_variant_panel(
                        clip_id, lang_code, dict(variants[lang_code]), result
                    )


def tab_batch_render() -> None:
    st.header("Batch Render & Preview")
    raw_files = sorted(RAW_DIR.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not raw_files:
        st.info("No clips in workspace/raw. Download or generate a test clip first.")
        return

    labels = [path.name for path in raw_files]
    selected = st.multiselect(
        "Clips to render",
        labels,
        default=labels,
        key="batch_selection",
    )
    codes = _supported_languages()
    config = load_config()
    default_language = normalize_language(config.get("default_language")) or "en"
    fallback_index = codes.index(default_language) if default_language in codes else 0
    control_cols = st.columns(3)
    with control_cols[0]:
        fallback_language = st.selectbox(
            "Fallback language for clips without metadata",
            codes,
            index=fallback_index,
            format_func=_language_label,
            key="render_fallback_language",
        )
    with control_cols[1]:
        translate_enabled = st.checkbox(
            "Also generate a translated version", value=False, key="render_translate"
        )
    with control_cols[2]:
        target_language = st.selectbox(
            "Translation target",
            codes,
            index=0,
            format_func=_language_label,
            key="render_target_language",
            disabled=not translate_enabled,
        )
    apply_uniqueness = st.checkbox(
        "Apply anti-duplicate fingerprinting",
        value=bool(config.get("anti_duplicate_enabled", True)),
        key="render_uniqueness",
        help="Subtle zoom, colour grade and 1.02x speed with resynced subtitles.",
    )
    layout_mode = st.session_state.get("layout_mode", "split")
    st.caption(
        "Layout: "
        + ("Split-Screen" if layout_mode == "split" else "Full-Screen blurred")
        + " | Native language first, translation optional"
    )

    if st.button("Render Selected Clips", type="primary", width="stretch"):
        if not selected:
            st.warning("Select at least one clip to render.")
        else:
            overall = st.progress(0.0, text="Starting batch render...")
            completed = 0
            for index, label in enumerate(selected):
                clip_id = Path(label).stem
                raw_path = RAW_DIR / label
                source_language = (
                    normalize_language(find_clip_language(clip_id)) or fallback_language
                )
                target = target_language if translate_enabled else None
                if target and normalize_language(target) == source_language:
                    target = None
                with st.status(f"Processing {clip_id}", expanded=True) as status:
                    try:
                        result = run_clip_pipeline(
                            clip_id,
                            raw_path,
                            source_lang=source_language,
                            target_lang=target,
                            game_name=str(config.get("default_game") or ""),
                            layout_mode=layout_mode,
                            preset_name=_preset_for_clip(clip_id),
                            cam_box=_cam_box_for_clip(clip_id),
                            config=_style_config(),
                            progress=status.write,
                            apply_uniqueness=apply_uniqueness,
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
    st.subheader("Output Gallery")
    results = collect_render_results()
    if not results:
        st.info("Rendered outputs will appear here with editable publishing metadata.")
        return
    for clip_id in sorted(results):
        render_clip_gallery(clip_id, results[clip_id])


def tab_settings_channels() -> None:
    st.header("Settings & Channels")
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
        if st.button("Save .env", width="stretch"):
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
        if st.button("Save Whisper Model", width="stretch"):
            _update_config_value("whisper_model", selected_model)
            st.success(f"Whisper model set to '{selected_model}'.")

        st.subheader("Fingerprinting")
        uniqueness_enabled = st.checkbox(
            "Enable anti-duplicate fingerprinting",
            value=bool(config.get("anti_duplicate_enabled", True)),
            key="settings_uniqueness_enabled",
        )
        if st.button("Save Fingerprinting Toggle", width="stretch"):
            _update_config_value("anti_duplicate_enabled", bool(uniqueness_enabled))
            st.success("Fingerprinting setting saved to config.yaml.")

    st.divider()
    st.subheader("YouTube channels")
    uploader = YouTubeUploader()
    codes = _supported_languages()
    channel_cols = st.columns(2)
    with channel_cols[0]:
        channel_choice = st.selectbox(
            "Channel language",
            [*codes, CUSTOM_LANGUAGE],
            format_func=lambda value: (
                "Custom ISO code…" if value == CUSTOM_LANGUAGE else _language_label(value)
            ),
            key="settings_channel_language",
        )
        channel_language = normalize_language(channel_choice)
        if channel_choice == CUSTOM_LANGUAGE:
            channel_custom = st.text_input(
                "Custom channel language code", value="en", key="settings_channel_custom"
            )
            channel_language = normalize_language(channel_custom)
        if not channel_language:
            st.warning("Enter a valid language code before connecting a channel.")
            channel_language = codes[0]

    statuses = uploader.token_status([channel_language])
    info = statuses[channel_language]
    with channel_cols[1]:
        st.markdown(
            f"#### {language_name(channel_language)} {language_badge(channel_language)}"
        )
        if info["exists"]:
            if info["valid"]:
                st.success("OAuth token connected and valid.")
            elif info["has_refresh_token"]:
                st.info("OAuth token present and refreshable.")
            else:
                st.warning("OAuth token present but expired without a refresh token.")
        else:
            st.warning("No OAuth token connected.")
        st.caption(f"Token file: {get_token_path(channel_language)}")
    if st.button(f"Connect {channel_language.upper()} channel", width="stretch"):
        if not CLIENT_SECRETS_FILE.exists():
            st.error(
                "Missing data/client_secret.json. Download an OAuth client from "
                "Google Cloud Console and save it there first."
            )
        else:
            try:
                token_path = authorize_channel(channel_language)
                st.success(f"Connected. Token saved to {token_path}.")
            except Exception as exc:  # noqa: BLE001 - surface OAuth UI failures
                st.error(f"OAuth connection failed: {exc}")

    connected = list_connected_languages()
    channel_codes = sorted(set(connected + ([channel_language] if channel_language else [])))
    quotas = uploader.check_quota_status(channel_codes)
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
    summary = "; ".join(
        f"{code.upper()} {quotas[code]['used_today']}/{quotas[code]['max_daily']}"
        for code in channel_codes
    )
    st.caption(
        "Hard guard: maximum 5 uploads per language channel per UTC day. "
        + (summary or "No channels connected yet.")
    )
    st.dataframe(
        [
            {
                "Channel": language_badge(code),
                "Language": language_name(code),
                "Connected": quotas[code]["connected"],
                "Max/day": quotas[code]["max_daily"],
                "Used today": quotas[code]["used_today"],
                "Remaining": quotas[code]["remaining"],
                "OK": quotas[code]["ok"],
            }
            for code in channel_codes
        ],
        width="stretch",
        hide_index=True,
    )
    if st.button("Refresh OAuth status", width="stretch"):
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="ClipForge Studio",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    ensure_runtime_dirs()
    _init_state()
    render_sidebar_health()
    fetch_tab, crop_tab, render_tab, settings_tab = st.tabs(
        [
            "Fetch & Queue",
            "Crop & Style",
            "Batch Render & Preview",
            "Settings & Channels",
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
