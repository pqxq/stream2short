# ClipForge Studio

**Open-source, multi-game, multi-language vertical clip automation.**

ClipForge Studio is a local-first Streamlit studio that turns gaming clips into polished `9:16` YouTube Shorts and TikToks. It is game-agnostic and language-first: fetch clips for any Twitch game, transcribe in the clip's native language, generate localized hooks/captions/CTAs, optionally translate into a second language, and publish to per-language YouTube channels.

## Features

- **Any game, any language niche** — dynamic Twitch game lookup plus ISO-639-1 language selection (`en`, `uk`, `es`, `de`, `fr`, `pl`, `pt`, `ru`, `ja`, `ko`, and custom codes).
- **Language-first discovery** — because Twitch `/helix/clips` has no language parameter, the fetcher paginates up to 10 pages of 100 clips and filters by `clip['language']` until your limit is reached.
- **Native transcription** — `faster-whisper` runs locally on CPU (`int8`) in the selected clip language.
- **Native metadata** — localized viral hook banner, YouTube title, description/hashtags and pinned-comment CTA for every supported language.
- **Optional translation** — add a second language variant through an OpenAI-compatible LLM, with a `deep-translator` + template fallback.
- **Vertical compositor** — split-screen webcam + gameplay or full-screen blurred-background `9:16` output with safe UTF-8 `drawtext` banner rendering.
- **Toggleable anti-duplicate fingerprinting** — subtle zoom, colour grade, speed change and exact subtitle time-scaling.
- **Per-language YouTube channels** — connect `data/token_<lang>.json` OAuth tokens for any language, schedule with `publishAt`, and keep a 5-uploads/day quota guard.
- **Zero-key verification** — generate a synthetic test clip and exercise the full pipeline without Twitch, LLM or YouTube credentials.

## Multi-Language Workflow

1. **Fetch** — choose a language (or All Languages), choose a game, then fetch Twitch clips or paste URLs.
2. **Style** — crop the webcam region, choose split/blur layout, and tune subtitle styling.
3. **Render** — render each clip in its native selected language. Optionally enable *Also generate a translated version* and choose a target language.
4. **Publish** — edit localized metadata per language variant and schedule uploads to the matching YouTube channel.

Outputs are written to:

```text
output/
├── en/<clip_id>_en.mp4
├── uk/<clip_id>_uk.mp4
├── es/<clip_id>_es.mp4
└── ...
```

## Installation (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install -y ffmpeg fonts-dejavu-core fonts-noto-cjk xdg-utils

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

streamlit run app.py
```

Python 3.10+ is required. The app is CPU-first and does not need a discrete GPU.

## API Setup

### Twitch (clip discovery)

1. Create an application in the [Twitch Developer Console](https://dev.twitch.tv/console/apps).
2. Copy `.env.example` to `.env` and set:
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`

Twitch clip fetching uses the app-access-token client-credentials flow.

### LLM (native metadata quality + optional translation)

Any OpenAI-compatible endpoint works (Groq, Gemini OpenAI-compatible, local gateways):

```env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.1-8b-instant
```

Without an LLM key, ClipForge uses localized templates and `deep-translator` where available. The pipeline never crashes because of an LLM failure.

### YouTube (per-language channels)

1. Enable the **YouTube Data API v3** in Google Cloud Console.
2. Create an OAuth **Desktop app** client and download it as `data/client_secret.json`.
3. In **Settings & Channels**, select a language and click **Connect <LANG> channel**.
4. Repeat for each language niche. Tokens are stored as `data/token_<lang>.json`.

Scheduling uses ISO-8601 `publishAt`, supports configurable spacing and slot indexes, and enforces a hard cap of **5 uploads per language channel per UTC day**.

## Configuration

Defaults live in `config.yaml`:

```yaml
default_game: "Grand Theft Auto VI"
default_language: "en"
supported_languages: [en, uk, es, de, fr, pl, pt, ru, ja, ko]

width: 1080
height: 1920
whisper_model: "small"
compute_type: "int8"

font_family: "DejaVu Sans"
font_size: 36
primary_color: "#FFFF00"
outline_thickness: 2
margin_v: 220

anti_duplicate_enabled: true
anti_duplicate_scale: 1.04
anti_duplicate_contrast: 1.03
anti_duplicate_saturation: 1.05
anti_duplicate_speed: 1.02
```

Webcam crop presets live in `presets.json` and can be edited from the **Crop & Style** tab.

## Architecture

```text
app.py                  Streamlit studio UI and pipeline orchestration
src/languages.py        ISO 639-1 normalization, names, badges, supported lists
src/fetcher.py          Twitch Helix game lookup, paginated language filtering, yt-dlp downloads
src/transcriber.py      faster-whisper, ASS subtitles, native metadata, optional translation
src/renderer.py         FFmpeg 9:16 compositor, UTF-8 textfile banners, fingerprinting toggle
src/uploader.py         YouTube Data API v3 uploads, dynamic per-language OAuth tokens
tests/smoke_test.py     Zero-key end-to-end EN + UK verification
```

### Anti-duplicate fingerprinting

When enabled, each rendered clip receives a subtle `1.04x` zoom, a micro colour grade, and `1.02x` playback speed. Video PTS uses `(1 / speed)`, audio uses `atempo=speed`, and ASS dialogue timestamps are multiplied by the exact reciprocal `1 / speed` so subtitles remain in sync. Disable it in `config.yaml` or from the render/settings controls.

## Verification

```bash
python3 -m py_compile app.py src/*.py tests/smoke_test.py
ruff check .

# Native EN + UK synthetic pipeline, no API keys required
.venv/bin/python tests/smoke_test.py --model tiny
```

The smoke test generates a five-second synthetic clip, runs whisper, builds native metadata/subtitles, renders `output/en/synth_test_en.mp4` and `output/uk/synth_test_uk.mp4`, and verifies H.264/AAC `1080x1920` output, distinct fingerprints and localized metadata.

## License

Released under the MIT License. See [`LICENSE`](LICENSE).
