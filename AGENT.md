# AGENT.md — `gta6-clip-factory`

## 1. Project Overview
`gta6-clip-factory` is a production-ready, dark-mode **Streamlit Web UI** application for Linux (Ubuntu/Debian) that automates turning viral gaming clips (Twitch & YouTube streams of *Grand Theft Auto VI*) into vertical `9:16` (`1080x1920`) YouTube Shorts and TikToks.

For every source clip, the pipeline:
1. Fetches clip metadata (Twitch Helix API or manual URLs) and downloads raw `.mp4` files via `yt-dlp`.
2. Transcribes speech locally on CPU via `faster-whisper` (`int8`) and generates `.ass` subtitles.
3. Translates subtitles to idiomatic Russian and generates viral hook banners via an OpenAI-compatible LLM endpoint (Groq/Gemini free tier) with automatic `deep-translator` fallback.
4. Composites vertical `9:16` split-screen (Webcam + Gameplay) or full-screen blurred-background videos via `ffmpeg`, outputting two uniquely fingerprinted versions per clip: English (`EN`) and Russian (`RU`).
5. Previews both outputs side-by-side and optionally schedules uploads to separate EN/RU YouTube channels via YouTube Data API v3.

---

## 2. Environment & System Constraints
- **Target OS:** Linux (Ubuntu / Debian).
- **System Dependencies:** `ffmpeg`, `fonts-dejavu-core` (or `fonts-liberation`), `xdg-utils` (`xdg-open`).
  - Default Linux font path for FFmpeg/ASS: `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` (font family `DejaVu Sans`).
- **Python Version:** `3.10+`
- **Hardware Target:** CPU-first execution (`faster-whisper` with `compute_type="int8"`, default model `"small"`). Must run smoothly without a discrete GPU.
- **Coding Standard:** **ZERO `# TODO` stubs, mock functions, or placeholders.** Every file must contain complete, runnable, error-handled production code.

---

## 3. Repository & Runtime Directory Structure

```text
gta6-clip-factory/
├── AGENT.md               # Project rules & architecture guide for AI coding agents
├── .streamlit/
│   └── config.toml        # Dark theme configuration & server.maxUploadSize
├── app.py                 # Main Streamlit GUI application (Sidebar + 4-tab studio)
├── config.yaml            # Default settings (1080x1920, whisper model, uniqueness params, margins)
├── presets.json           # Streamer webcam crop coordinates {streamer: {x, y, w, h}} + "default"
├── .env.example           # Template for TWITCH_*, LLM_*, and model env vars
├── requirements.txt       # Pinned compatible Python dependencies
├── README.md              # Setup instructions including `sudo apt install ffmpeg fonts-dejavu-core`
├── src/
│   ├── __init__.py
│   ├── fetcher.py         # Twitch Helix API top-clips fetcher + yt-dlp downloader + JSON deduplication
│   ├── transcriber.py     # faster-whisper -> .ass generator + EN->RU translation & viral hooks
│   ├── renderer.py        # FFmpeg frame extractor, 9:16 compositor & EN/RU anti-duplicate filters
│   └── uploader.py        # YouTube Data API v3 scheduled uploader (`publishAt` spacing & quota guard)
├── data/                  # [Runtime] processed_clips.json, token_en.json, token_ru.json
├── workspace/             # [Runtime] raw/<clip_id>.mp4, temp/ (frames, .ass, hook .txt files)
└── output/                # [Runtime] en/<clip_id>_en.mp4, ru/<clip_id>_ru.mp4
```

*Note:* Ensure all runtime directories (`data/`, `workspace/raw/`, `workspace/temp/`, `output/en/`, `output/ru/`) are created programmatically on startup via `Path(...).mkdir(parents=True, exist_ok=True)`.

---

## 4. Build, Run & Verification Commands

```bash
# 1. Install OS dependencies (Ubuntu/Debian)
sudo apt update && sudo apt install -y ffmpeg fonts-dejavu-core

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Syntax & import verification (REQUIRED after any code change)
python3 -m py_compile app.py src/*.py

# 4. Launch Streamlit application
streamlit run app.py
```

---

## 5. Non-Negotiable Backend Rules (`src/`)

### Rule 1: FFmpeg Safe UTF-8 Text Rendering (`src/renderer.py`)
- **NEVER** pass dynamic hook banner strings inline inside `-vf drawtext=text='...'`. Cyrillic characters, apostrophes, colons, or quotes will break FFmpeg filtergraph parsing.
- **ALWAYS** write the hook text to a temporary UTF-8 file (e.g., `/tmp/hook_<clip_id>_<lang>.txt` or `workspace/temp/hook_<clip_id>_<lang>.txt`) and pass `textfile=<escaped_path>:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` to `drawtext`.

### Rule 2: Anti-Duplicate Fingerprinting (`src/renderer.py`)
Every clip must produce two visually/acoustically distinct perceptual hashes while preserving subtitle sync:
- **English (`output/en/<clip_id>_en.mp4`):**
  - Scale: `1.0x`
  - Playback speed: `1.0x` (standard PTS & audio tempo)
  - Subtitles: Original `.ass` timestamps (`1.0x`)
- **Russian (`output/ru/<clip_id>_ru.mp4`):**
  - Subtle zoom: `1.04x` (scale up by 1.04 and center-crop back to `1080x1920`)
  - Micro color grade: `eq=contrast=1.03:saturation=1.05`
  - Playback speed: `1.02x` (`setpts=0.98*PTS` on video, `atempo=1.02` on audio)
  - Subtitle sync: Multiply all `.ass` dialogue start and end timestamps by `0.98` so subtitles remain frame-accurate at `1.02x` playback speed.
- **Encoding Standard:** `libx264`, `-preset fast`, `-crf 21`, `-pix_fmt yuv420p`, audio `aac` `128k`.

### Rule 3: Subtitle Formatting & Wrapping (`src/transcriber.py`)
- Output `.ass` (Advanced SubStation Alpha) subtitle files configured for `PlayResX: 1080`, `PlayResY: 1920`, using `DejaVu Sans`.
- **Line Wrapping:** Wrap subtitle lines at **maximum 28–32 characters per line** (breaking cleanly at word boundaries using `\N`) so bold captions never overflow horizontally on mobile `9:16` screens.
- **Safe Margins:** Default vertical bottom margin `MarginV = 220` so subtitles sit above TikTok / YouTube Shorts bottom UI buttons.

### Rule 4: Resilient Translation & Hook Pipeline (`src/transcriber.py`)
- Primary path: Use the `openai` Python SDK configured with `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` from `.env` (compatible with Groq / Gemini OpenAI-compatible endpoints) to:
  1. Translate transcribed segments into natural, gaming-slang-aware Russian.
  2. Generate punchy EN and RU viral hook banner headlines (max ~45 chars).
- Fallback path: If `LLM_API_KEY` is missing, invalid, or throws an API/rate-limit error (`429`), seamlessly fall back to `deep-translator` (`GoogleTranslator(source="auto", target="ru")`) and template-based hooks without crashing the batch job.

### Rule 5: Fetcher & Deduplication (`src/fetcher.py`)
- Support Twitch Helix API (`OAuth2 client_credentials` token acquisition + `/helix/games` + `/helix/clips`) and direct Twitch/YouTube URLs via `yt-dlp`.
- Track processed clip IDs and metadata in `data/processed_clips.json`. Allow force re-download/re-processing if explicitly checked by the user in the UI.

### Rule 6: YouTube Scheduled Uploader (`src/uploader.py`)
- Support separate OAuth2 credential tokens (`data/token_en.json` and `data/token_ru.json`) using `google-api-python-client` and `google-auth-oauthlib`.
- Enforce spacing via ISO-8601 `publishAt` timestamps (`start_time` + `N * interval_hours`) and a hard daily quota guard (**max 5 uploads/day** per channel).

---

## 6. GUI Architecture (`app.py`)

### Global & Sidebar Requirements
- Configure dark theme in `.streamlit/config.toml` (`base="dark"`).
- Store clip queues, selection states, crop coordinates, and rendered metadata in `st.session_state` so widget interactions never reset active workflows.
- **Sidebar:**
  - System health indicators: FFmpeg binary check, Whisper model status, Processed Clips counter.
  - **"Generate Synthetic Test Clip"** button: Generates a 5-second `1280x720` test video (`testsrc` + `sine` audio via FFmpeg) and runs the full transcription + EN/RU rendering pipeline end-to-end so the app can be verified immediately with zero API keys.

### Tab 1: Fetch & Queue (`Збір кліпів`)
- **Manual URLs:** Multi-line `st.text_area` for Twitch clip or YouTube URLs.
- **Twitch Auto-Fetcher:** Inputs for Game Name (default `"Grand Theft Auto VI"`), Timeframe (`24h`, `7d`), Clip Limit (`1–20`), and **"Fetch Top Clips"** button.
- **Interactive Clip Queue:** Cards/rows displaying thumbnail, streamer username, title, view count, duration, selection checkbox, and a `Processed` badge if found in `data/processed_clips.json` (with a "Force Re-download" option).
- **"Download Selected to Workspace"** button with `st.progress` saving to `workspace/raw/<clip_id>.mp4`.

### Tab 2: Crop & Style Studio (`Рамка вебкамери та стиль`)
- Select any clip from `workspace/raw/` and auto-load the streamer's preset from `presets.json` (fallback to `"default"`).
- Controls:
  - 4 sliders (`cam_x`, `cam_y`, `cam_w`, `cam_h` from `0.0` to `1.0`).
  - Layout mode toggle: **Split-Screen (Webcam + Gameplay)** vs **Full-Screen 9:16 Gameplay with Blurred BG**.
  - Subtitle & Hook styling: font size, primary color (`#FFFF00`, `#FFFFFF`, etc.), outline thickness, and `MarginV` (default `220`).
- **Live Visual Preview:** Extract middle frame via FFmpeg and render two side-by-side `Pillow` images:
  1. Original `16:9` frame with a **bright green** rectangle (webcam crop) and **blue** rectangle (gameplay crop).
  2. Resulting `9:16` composite preview (`1080x1920` scaled for UI) showing sample hook banner and subtitle placement.
- **"Save Preset for Streamer"** button persisting coordinates into `presets.json`.

### Tab 3: Batch Render & Preview (`Рендер та перевірка`)
- **"Render Selected Clips (EN + RU)"** button running `transcriber.py` and `renderer.py` sequentially with `st.progress` and `st.status` step logging (`Transcribing -> Translating -> Rendering EN -> Rendering RU`).
- **Side-by-Side Output Gallery:**
  - Two columns per clip: **English Version (EN)** on the left, **Russian Version (RU)** on the right.
  - Native `st.video()` players for `output/en/<clip_id>_en.mp4` and `output/ru/<clip_id>_ru.mp4`.
  - Editable fields below each player: Hook Banner Text, Video Title, Description & Hashtags, and Pinned Comment CTA (pre-filled with Telegram funnel template for `RU` and engagement/affiliate template for `EN`).
  - Action buttons per video: **"Re-render Hook Text"**, **"Open Output Folder"** (triggers `xdg-open`), and **"Schedule to YouTube"**.

### Tab 4: Settings & Channels (`Налаштування та API`)
- Masked credential editor for `.env` (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`) with a **"Save .env"** button.
- Whisper model selector (`tiny`, `base`, `small`, `medium`) persisted to `config.yaml`.
- YouTube OAuth connection status indicators (`token_en.json`, `token_ru.json`) + scheduling controls (`start_time`, `interval_hours`, max 5 uploads/day guard).

---

## 7. Definition of Done (Verification Checklist)
Before completing any task on this repository:
1. Run `python3 -m py_compile app.py src/*.py` and confirm exit code `0` with zero syntax errors.
2. Verify all runtime directories and default config files (`config.yaml`, `presets.json`, `.env.example`, `.streamlit/config.toml`) exist and parse cleanly.
3. Verify the **"Generate Synthetic Test Clip"** flow creates a 5-second synthetic `.mp4`, executes `transcriber.py` and `renderer.py`, and produces valid, playable `output/en/<id>_en.mp4` and `output/ru/<id>_ru.mp4` files with distinct durations/hashes (`1.0x` vs `1.02x`) and burned-in UTF-8 hook + subtitles.

## Autonomous Execution & Delegation Policy
- Act as an autonomous Tech Lead: break the build into sequential milestones (1. Configs & Skeleton -> 2. src/fetcher.py -> 3. src/transcriber.py -> 4. src/renderer.py -> 5. src/uploader.py -> 6. app.py -> 7. End-to-End Verification).
- Delegate repository inspection and file discovery to the `explore` subagent.
- Delegate architectural design, FFmpeg filtergraph validation, and root-cause debugging to the `architect` subagent.
- Delegate module code generation to the `coder` subagent to balance throughput across free providers.
- After completing each milestone, run `python3 -m py_compile app.py src/*.py` (for existing files) and `ruff check .`, fix all issues immediately, and create a local `git commit` checkpoint before starting the next module.
