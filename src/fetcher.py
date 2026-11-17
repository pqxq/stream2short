import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
PROCESSED_CLIPS_FILE = DATA_DIR / "processed_clips.json"

# Default Twitch API endpoints
TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"


def load_processed_clips() -> list[dict[str, Any]]:
    """Load processed clips from JSON file. Returns empty list if file not found."""
    if PROCESSED_CLIPS_FILE.exists():
        with open(PROCESSED_CLIPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_processed_clips(clips: list[dict[str, Any]]) -> None:
    """Save processed clips to JSON file."""
    with open(PROCESSED_CLIPS_FILE, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2)


def get_twitch_token() -> str | None:
    """Obtain OAuth2 client_credentials token from Twitch."""
    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error("Twitch credentials missing; disabling Twitch API fetching.")
        return None

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }
    try:
        response = requests.post(TWITCH_OAUTH_URL, data=data)
        response.raise_for_status()
        return response.json()["access_token"]
    except Exception as e:
        logger.error("Failed to obtain Twitch token: %s", e)
        return None


def fetch_twitch_clips(game_name: str, first: int = 10, period: str = "7d") -> list[dict[str, Any]]:
    """Fetch top clips for a given game name via Twitch Helix API."""
    token = get_twitch_token()
    if not token:
        logger.warning("Twitch API unavailable (missing credentials).")
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "Client-ID": os.getenv("TWITCH_CLIENT_ID")
    }

    # Search for game ID
    games_url = f"{TWITCH_API_BASE}/games?category_exact={requests.utils.quote(game_name)}"
    game_resp = requests.get(games_url, headers=headers)
    game_resp.raise_for_status()
    game_data = game_resp.json().get("data", [])
    if not game_data:
        logger.warning("No game found for '%s'", game_name)
        return []
    game_id = game_data[0]["id"]

    # Fetch clips for that game
    clips_url = f"{TWITCH_API_BASE}/clips?game_id={game_id}&first={first}&period={period}&is_relevant=True"
    clips_resp = requests.get(clips_url, headers=headers)
    clips_resp.raise_for_status()
    return clips_resp.json().get("data", [])


def download_clip_with_ytdlp(url: str, output_dir: Path) -> Path | None:
    """Download a clip using yt-dlp. Returns the downloaded file path or None on failure."""
    cmd = [
        "yt-dlp",
        "--output", f"{output_dir / '%f'}.mp4",
        "--ignore-config",
        "--no-overwrite",
        "--continue",
        "--verbose",
        url
    ]
    try:
        subprocess.run(cmd, check=True)
        # yt-dlp writes to output_dir with name like <filename>.mp4
        downloaded_files = list(output_dir.glob("*.mp4"))
        if not downloaded_files:
            logger.error("yt-dlp did not produce a file for URL %s", url)
            return None
        # Assuming yt-dlp creates a single file; return first match
        return downloaded_files[0]
    except subprocess.CalledProcessError as e:
        logger.error("yt-dlp failed to download %s: %s", url, e)
        return None


def process_and_queue_clip(clip_data: dict[str, Any], raw_dir: Path) -> bool:
    """
    Process a clip: extract metadata, download if needed, and update processed_clips.json.
    Returns True if clip was successfully processed.
    """
    clip_id = clip_data.get("id")
    if not clip_id:
        logger.error("Clip missing id: %s", clip_data)
        return False

    # Load existing processed clips to check for duplicates
    processed = load_processed_clips()
    processed_ids = {c.get("id") for c in processed if c.get("id")}
    is_duplicate = clip_id in processed_ids

    # Save clip metadata to data/processed_clips.json regardless of re-download
    processed.append(clip_data)
    save_processed_clips(processed)

    if is_duplicate:
        logger.info("Clip %s already processed (duplicate). Skipping download.", clip_id)
        return True

    # If not duplicate and manual URL, we cannot auto-download; return True because metadata saved
    if "url" not in clip_data:
        logger.info("Clip %s has no URL, only metadata saved.", clip_id)
        return True

    # Download raw clip to workspace/raw/<clip_id>.mp4
    url = clip_data["url"]
    downloaded = download_clip_with_ytdlp(url, raw_dir)
    if not downloaded:
        logger.error("Failed to download clip %s", clip_id)
        return False

    logger.info("Clip %s downloaded to %s", clip_id, downloaded)
    return True
