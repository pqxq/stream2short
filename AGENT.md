# AGENT.md — ClipForge Studio

## 1. Project Overview
ClipForge Studio is an open-source, dark-mode **Streamlit** studio for Linux (Ubuntu/Debian) that turns gaming clips from any Twitch/YouTube source into vertical `9:16` (`1080x1920`) Shorts/TikToks for **any supported language niche**.

The pipeline is language-first and game-agnostic:
1. Resolve a game dynamically via Twitch Helix `/helix/games` and fetch clips with client-side language filtering.
2. Download raw media with `yt-dlp` and keep a deduplicated processed-clip index.
3. Transcribe locally with `faster-whisper` (`int8`, CPU-first) in the clip's selected language (native mode).
4. Generate `.ass` subtitles plus a native-language hook banner, title, description/hashtags and pinned-comment CTA.
5. Optionally translate subtitles and metadata into a second target language via an OpenAI-compatible LLM, with a `deep-translator` fallback.
6. Composite split-screen (webcam + gameplay) or blurred-background `9:16` video with `ffmpeg`.
7. Optionally apply anti-duplicate fingerprinting and schedule uploads to per-language YouTube channels.

No game, language pair, or channel count is hardcoded. Defaults come from `config.yaml`.

---

## 2. Environment & System Constraints
- **Target OS:** Linux (Ubuntu / Debian).
- **System Dependencies:** `ffmpeg`, `fonts-dejavu-core` (or `fonts-liberation`), optional `fonts-noto-cjk` for Japanese/Korean, and `xdg-utils` (`xdg-open`).
- **Python Version:** `3.10+`.
- **Hardware Target:** CPU-first (`faster-whisper` `compute_type="int8"`, default model `"small"`).
- **Coding Standard:** **ZERO `# TODO` stubs, mock functions, or placeholders.** Every file must contain complete, runnable, error-handled production code.

---

## 3. Repository & Runtime Directory Structure

```text
clipforge-studio/
├── AGENT.md               # Project rules & architecture guide for AI coding agents
├── .streamlit/
│   └── config.toml        # Dark theme configuration & server.maxUploadSize
├── app.py                 # Main Streamlit studio (Sidebar + 4 tabs)
├── config.yaml            # default_game/default_language/supported_languages/style/uniqueness
├── presets.json           # Streamer webcam crop coordinates {streamer: {x, y, w, h}} + "default"
├── .env.example           # Template for TWITCH_*, LLM_*, model env vars
├── requirements.txt       # Pinned compatible Python dependencies
├── README.md              # Setup, API configuration and architecture overview
├── src/
│   ├── __init__.py
│   ├── languages.py       # ISO 639-1 normalization, names, badges, supported lists
│   ├── fetcher.py         # Twitch Helix game lookup + paginated language-first clip fetcher + yt-dlp
│   ├── transcriber.py     # faster-whisper + ASS + native metadata + optional translation
│   ├── renderer.py        # FFmpeg 9:16 compositor + toggleable anti-duplicate fingerprinting
│   └── uploader.py        # YouTube Data API v3 uploader with dynamic per-language tokens
├── data/                  # [Runtime] processed_clips.json, token_<lang>.json, upload_history.json
├── workspace/             # [Runtime] raw/<clip_id>.mp4, temp/ (audio, .ass, hook .txt, localization JSON)
└── output/                # [Runtime] <lang>/<clip_id>_<lang>.mp4
```

Runtime directories are created programmatically on startup via `Path(...).mkdir(parents=True, exist_ok=True)`.

---

## 4. Build, Run & Verification Commands

```bash
# 1. Install OS dependencies (Ubuntu/Debian)
sudo apt update && sudo apt install -y ffmpeg fonts-dejavu-core fonts-noto-cjk

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Syntax verification (REQUIRED after any code change)
python3 -m py_compile app.py src/*.py tests/smoke_test.py

# 4. Lint (REQUIRED after any code change)
ruff check .

# 5. Zero-key end-to-end smoke test (native EN + UK)
.venv/bin/python tests/smoke_test.py --model tiny

# 6. Launch Streamlit
streamlit run app.py
```

---

## 5. Non-Negotiable Backend Rules (`src/`)

### Rule 1: FFmpeg Safe UTF-8 Text Rendering (`src/renderer.py`)
- **NEVER** pass dynamic hook banner strings inline inside `-vf drawtext=text='...'`.
- **ALWAYS** write hook text to a temporary UTF-8 file and pass `textfile=<escaped_path>:fontfile=<escaped_font_path>:expansion=none` to `drawtext`.
- Use `_filter_path()` for escaping paths and quote filter inputs.
- Japanese/Korean renders should use a Noto CJK font when available; otherwise fall back to DejaVu/Liberation.

### Rule 2: Toggleable Anti-Duplicate Fingerprinting (`src/renderer.py`)
Fingerprinting is configured in `config.yaml`:
```yaml
anti_duplicate_enabled: true
anti_duplicate_scale: 1.04
anti_duplicate_contrast: 1.03
anti_duplicate_saturation: 1.05
anti_duplicate_speed: 1.02
```
When enabled for a render:
- Subtle zoom: scale by `anti_duplicate_scale` and center-crop back to `1080x1920`.
- Micro colour grade: `eq=contrast=<...>:saturation=<...>`.
- Playback speed: `setpts=(1/speed)*PTS` on video and `atempo=<speed>` on audio.
- Subtitle sync: multiply ASS dialogue timestamps by the **exact reciprocal** `1/speed`, not a rounded `0.98`.
- When disabled, output the original timing, no zoom and no colour grade.
- **Encoding Standard:** `libx264`, `-preset fast`, `-crf 21`, `-pix_fmt yuv420p`, audio `aac` `128k`.
- Output path: `output/<lang_code>/<clip_id>_<lang_code>.mp4`.

### Rule 3: Subtitle Formatting & Wrapping (`src/transcriber.py`)
- Output `.ass` subtitle files configured for `PlayResX: 1080`, `PlayResY: 1920`.
- Wrap subtitle lines at **maximum 28–32 characters per line** at word boundaries using `\N`.
- Default safe bottom margin `MarginV = 220`.
- Use the clip's selected language for native transcription (`language=source_lang`; `None` for auto).

### Rule 4: Native-First Localisation (`src/transcriber.py`)
- **Native mode is the default:** transcribe and generate hook/title/description/hashtags/CTA in the selected clip language. Localized templates exist for `en, uk, es, de, fr, pl, pt, ru, ja, ko`; custom codes fall back safely.
- **Translate mode is optional:** if `target_lang` is explicitly set and differs from the source language, translate subtitles and metadata via an OpenAI-compatible LLM.
- LLM prompt must request strict JSON and validate translation array length.
- Fallback path: `deep-translator` (`GoogleTranslator(source=..., target=...)`), then localized templates.
- Never claim a target variant is translated when subtitles remain in the source language; mark its `mode`/`source`.

### Rule 5: Language-First Fetcher (`src/fetcher.py`)
- Resolve the game dynamically via Twitch Helix `/helix/games`.
- **Twitch `/helix/clips` has no `language` query parameter.** Request `first=100` pages with the `after` cursor (up to 10 pages) and filter `clip['language'] == target_language` until the requested limit is reached.
- Include each clip's detected `language` in normalized metadata and workspace queue items.
- Keep `data/processed_clips.json` deduplication and explicit force re-download.

### Rule 6: Dynamic Per-Language YouTube Uploader (`src/uploader.py`)
- Store OAuth2 tokens at `data/token_<lang_code>.json` for any ISO 639-1 code.
- Keep ISO-8601 `publishAt` spacing and a hard daily quota guard (**max 5 uploads/day** per language channel).
- `defaultLanguage`/`defaultAudioLanguage` and upload history are keyed by the token's language.

---

## 6. GUI Architecture (`app.py`)

### Global & Sidebar Requirements
- `st.set_page_config(page_title="ClipForge Studio", layout="wide")`.
- Persist clip queues, selection states, crop coordinates and render metadata in `st.session_state`.
- **Sidebar:** FFmpeg check, Whisper status, processed-clip count, active pipeline language, and **"Generate Synthetic Test Clip"** which runs the full native pipeline for the currently selected language with zero API keys.

### Tab 1: Fetch & Queue
- **Step 1 — Language:** All Languages, popular ISO-639-1 languages, and a custom code input.
- **Step 2 — Game:** free-text game name with quick-select presets (GTA VI, Counter-Strike 2, Valorant, Dota 2, Minecraft, Just Chatting).
- Manual URL queue plus Twitch auto-fetch (timeframe + limit).
- Clip cards show thumbnail, title, language badge `[EN]`/`[UK]`/etc., game, streamer, views, duration, processed badge, force re-download, and selection.

### Tab 2: Crop & Style
- Load streamer presets from `presets.json` (fallback `"default"`).
- Four normalized crop sliders, split/blur layout toggle, subtitle/hook styling.
- Live Pillow preview of source crop and resulting `9:16` composite.

### Tab 3: Batch Render & Preview
- Render selected clips in their **native selected language**.
- Optional **"Also generate a translated version"** toggle with target language selector.
- Anti-duplicate fingerprinting checkbox.
- Responsive per-language preview cards with editable localized Hook, Title, Description and CTA.
- Per-variant **Re-render Hook**, **Open Folder**, and **Schedule to YouTube** actions.

### Tab 4: Settings & Channels
- Masked `.env` editor for Twitch/LLM credentials.
- Whisper model selector and fingerprinting toggle persisted to `config.yaml`.
- Dynamic YouTube OAuth management for any selected language code (`data/token_<lang>.json`).
- Scheduling controls (`start_time`, `interval_hours`, slot index) and dynamic per-language quota table.

---

## 7. Definition of Done (Verification Checklist)
Before completing any task:
1. `python3 -m py_compile app.py src/*.py tests/smoke_test.py` exits `0`.
2. `ruff check .` passes with no errors.
3. Runtime directories and default config files parse cleanly.
4. The synthetic test creates a 5-second clip and produces valid `output/en/synth_test_en.mp4` and `output/uk/synth_test_uk.mp4` with distinct fingerprints, native metadata, `.ass` subtitles and UTF-8 hooks.
5. No hardcoded GTA VI-only, EN/RU-only, or `token_en`/`token_ru` assumptions remain outside default presets/config values.

## Autonomous Execution & Delegation Policy
- Act as an autonomous Tech Lead: discover, design, implement, verify, and commit only when requested.
- Delegate repository inspection/file discovery to the `explore` subagent when available.
- Delegate architecture/FFmpeg filtergraph review and root-cause debugging to the `architect` subagent.
- Delegate module code generation to the `coder` subagent when providers are healthy; if unavailable, implement directly and verify thoroughly.
- After each milestone, run `python3 -m py_compile ...` and `ruff check .`, fix all issues immediately, then continue.
