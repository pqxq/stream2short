# ShortStack

ShortStack is a local-first Streamlit studio that turns gaming clips into 9:16 YouTube Shorts and TikToks. It fetches clips for any Twitch game, transcribes them in their native language, renders vertical composites with FFmpeg, and optionally schedules uploads to per-language YouTube channels.

![ShortStack preview](docs/preview.png)

## Capabilities

- **Multi-language Twitch discovery.** Resolves any Twitch game by name, paginates clip results, and filters by ISO 639-1 language because the Twitch clips endpoint has no language parameter.
- **CPU int8 Whisper transcription.** Runs `faster-whisper` locally on CPU with `compute_type="int8"`. No GPU is required.
- **Interactive 9:16 crop studio.** Adjusts the webcam region, layout, subtitle typography, and safe zone with a live source and composite preview.
- **FFmpeg split-screen compositing.** Produces 1080x1920 output with split-screen webcam/gameplay or a full-screen blurred background.
- **Optional YouTube scheduling.** Connects per-language OAuth channels, uploads with `publishAt`, and enforces a five-upload daily quota per channel.
- **Zero-key verification.** Renders a synthetic clip through the complete pipeline without Twitch, LLM, or YouTube credentials.

## Pipeline

```text
Twitch Helix or public URL
            |
            v
   yt-dlp download
   workspace/raw/<clip_id>.mp4
            |
            v
   faster-whisper (CPU int8)
   .ass subtitles + localized metadata
            |
            v
   FFmpeg 9:16 compositor
   output/<lang>/<clip_id>_<lang>.mp4
            |
            +--> optional anti-duplicate fingerprinting
            |
            +--> optional YouTube publishAt scheduling
```

## Quickstart (Ubuntu / Debian)

1. Install system packages.

   ```bash
   sudo apt update
   sudo apt install -y ffmpeg fonts-dejavu-core fonts-noto-cjk xdg-utils
   ```

2. Create a virtual environment and install Python dependencies.

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure optional credentials.

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

   Twitch credentials enable clip discovery. LLM credentials enable model-generated metadata and translation. Public Twitch and YouTube URLs work without credentials.

4. Launch the studio.

   ```bash
   streamlit run app.py
   ```

## Usage

The workspace is organized into four tabs:

1. **Fetch & Queue** selects a language and game, then imports public URLs or fetches Twitch clips.
2. **Crop & Style** sets the webcam crop, layout, subtitle styling, and streamer presets.
3. **Batch Render & Preview** renders selected clips, edits publishing metadata, and schedules uploads.
4. **Settings & Channels** stores credentials, selects the Whisper model, and connects YouTube channels.

Without Twitch credentials, use **Load Demo Clip** in the first tab to render a synthetic clip immediately.

Outputs are written per language:

```text
output/
├── en/<clip_id>_en.mp4
├── uk/<clip_id>_uk.mp4
├── es/<clip_id>_es.mp4
└── ...
```

Webcam crops are stored in `presets.json`. YouTube OAuth tokens and upload history are stored under `data/`.

## Configuration

### Environment variables (`.env`)

| Variable | Required | Description |
| --- | --- | --- |
| `TWITCH_CLIENT_ID` | For Twitch discovery | Client ID from the Twitch Developer Console. |
| `TWITCH_CLIENT_SECRET` | For Twitch discovery | Client secret for the app access token flow. |
| `LLM_API_KEY` | No | API key for an OpenAI-compatible endpoint. |
| `LLM_BASE_URL` | No | OpenAI-compatible base URL. Defaults to `https://api.groq.com/openai/v1`. |
| `LLM_MODEL` | No | Metadata and translation model name. Defaults to `llama-3.1-8b-instant`. |

Without an LLM key, ShortStack uses localized templates and `deep-translator` where available.

### `config.yaml`

| Key | Default | Description |
| --- | --- | --- |
| `default_game` | `Grand Theft Auto VI` | Game used by the fetcher and metadata templates. |
| `default_language` | `en` | Source language when a clip has no stored language. |
| `supported_languages` | `[en, uk, es, de, fr, pl, pt, ru, ja, ko]` | Language codes shown in the UI. |
| `width` / `height` | `1080` / `1920` | Output resolution (9:16). |
| `whisper_model` | `small` | CPU transcription model: `tiny`, `base`, `small`, or `medium`. |
| `compute_type` | `int8` | `faster-whisper` compute type. |
| `font_family` | `DejaVu Sans` | Subtitle font family. |
| `font_size` | `36` | Subtitle text size at 1080x1920. |
| `primary_color` | `#FFFF00` | Subtitle fill color. |
| `outline_thickness` | `2` | Subtitle outline width in pixels. |
| `margin_v` | `220` | Bottom safe zone in pixels. |
| `anti_duplicate_enabled` | `true` | Enables subtle zoom, colour grade, and speed changes. |
| `anti_duplicate_scale` | `1.04` | Fingerprint zoom factor. |
| `anti_duplicate_contrast` | `1.03` | Fingerprint contrast factor. |
| `anti_duplicate_saturation` | `1.05` | Fingerprint saturation factor. |
| `anti_duplicate_speed` | `1.02` | Playback speed factor. Subtitle timestamps are rescaled to match. |

### YouTube OAuth

1. Enable the YouTube Data API v3 in Google Cloud Console.
2. Create an OAuth Desktop app client and save it as `data/client_secret.json`.
3. Open **Settings & Channels**, select a language, and connect the channel.
4. Repeat for each language. Tokens are stored as `data/token_<lang>.json`.

Scheduling uses ISO 8601 `publishAt` timestamps with configurable spacing and a hard limit of five uploads per language channel per UTC day.

## Architecture

| Path | Responsibility |
| --- | --- |
| `app.py` | Streamlit studio UI and pipeline orchestration. |
| `src/languages.py` | ISO 639-1 normalization, names, badges, and supported language lists. |
| `src/fetcher.py` | Twitch Helix game lookup, paginated language filtering, and yt-dlp downloads. |
| `src/transcriber.py` | faster-whisper, ASS subtitles, localized metadata, and optional translation. |
| `src/renderer.py` | FFmpeg 9:16 compositor, UTF-8 textfile banners, and fingerprinting. |
| `src/uploader.py` | YouTube Data API v3 uploads with per-language OAuth tokens. |
| `tests/smoke_test.py` | Zero-key end-to-end English and Ukrainian verification. |

## Verification

```bash
.venv/bin/python -m py_compile app.py src/*.py tests/smoke_test.py
.venv/bin/ruff check .

# Native EN and UK synthetic pipeline, no API keys required
.venv/bin/python tests/smoke_test.py --model tiny
```

The smoke test generates a five-second clip, runs Whisper, builds native metadata and subtitles, renders `output/en/synth_test_en.mp4` and `output/uk/synth_test_uk.mp4`, and verifies H.264/AAC 1080x1920 output, distinct fingerprints, and localized metadata.

## License

Released under the MIT License. Copyright (c) 2026 ShortStack contributors. See [`LICENSE`](LICENSE).
