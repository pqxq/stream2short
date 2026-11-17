# GTA6 Clip Factory

Production-ready Streamlit web UI that automates viral gaming clips into vertical YouTube Shorts & TikToks.

## Setup

Install OS dependencies (Ubuntu/Debian):

```bash
sudo apt update && sudo apt install -y ffmpeg fonts-dejavu-core
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Launch Streamlit application:

```bash
streamlit run app.py
```

## Usage

- Use **"Generate Synthetic Test Clip"** button in sidebar for zero-API-key verification.
- Use Twitch Auto-Fetcher or manual URLs in "Fetch & Queue" tab.
- Use "Render Selected Clips (EN + RU)" to process clips with EN/RU anti-duplicate outputs.
- Use scheduled YouTube uploads in "Settings & Channels" tab.

## Architecture

- **src/fetcher.py**: Twitch Helix API + yt-dlp downloader + JSON deduplication.
- **src/transcriber.py**: faster-whisper -> .ass generator + EN->RU translation + viral hooks.
- **src/renderer.py**: FFmpeg frame extractor, 9:16 compositor + EN/RU anti-duplicate filters.
- **src/uploader.py**: YouTube Data API v3 scheduled uploader with daily quota guard.

All runtime directories are created programmatically on startup: `data/`, `workspace/raw/`, `workspace/temp/`, `output/en/`, `output/ru/`.

## Verification

After any code change, run:

```bash
python3 -m py_compile app.py src/*.py tests/smoke_test.py
ruff check .
```

Automated end-to-end smoke test (synthetic 5s clip, no API keys required):

```bash
.venv/bin/python tests/smoke_test.py --model small --keep
```

It verifies both `output/en/synth_test_en.mp4` and `output/ru/synth_test_ru.mp4`
are playable 1080x1920 H.264/AAC files with distinct durations and fingerprints.
