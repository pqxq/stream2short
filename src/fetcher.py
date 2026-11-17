"""Clip discovery and download helpers.

This module talks to the Twitch Helix API, downloads clips with yt-dlp and
keeps a persistent deduplication index in ``data/processed_clips.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yt_dlp

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PROCESSED_CLIPS_FILE = DATA_DIR / "processed_clips.json"

TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"

DEFAULT_GAME = "Grand Theft Auto VI"
VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".flv")
REQUEST_TIMEOUT = 30


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _safe_clip_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "clip"


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def _normalize_twitch_clip(clip: dict[str, Any], game_name: str) -> dict[str, Any]:
    clip_id = str(clip.get("id") or "").strip()
    url = clip.get("url") or (f"https://clips.twitch.tv/{clip_id}" if clip_id else "")
    created_at = clip.get("created_at") or ""
    return {
        "id": clip_id,
        "url": url,
        "title": clip.get("title") or "Untitled clip",
        "streamer": clip.get("broadcaster_name") or clip.get("creator_name") or "unknown",
        "thumbnail": clip.get("thumbnail_url") or "",
        "views": int(clip.get("view_count") or 0),
        "duration": float(clip.get("duration") or 0.0),
        "game": game_name,
        "created_at": created_at,
        "source": "twitch",
    }


def _guess_clip_id_from_url(url: str) -> str:
    path = url.split("?", 1)[0].rstrip("/")
    candidate = path.rsplit("/", 1)[-1]
    candidate = candidate.split("#", 1)[0]
    return _safe_clip_id(candidate or "clip")


def load_processed_clips() -> list[dict[str, Any]]:
    """Load processed clip metadata, tolerating missing or corrupt files."""
    if not PROCESSED_CLIPS_FILE.exists():
        return []
    try:
        with PROCESSED_CLIPS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", PROCESSED_CLIPS_FILE, exc)
        return []
    if isinstance(data, dict):
        data = data.get("clips", [])
    if not isinstance(data, list):
        logger.warning("Unexpected processed-clips payload; ignoring it.")
        return []
    return [item for item in data if isinstance(item, dict)]


def save_processed_clips(clips: list[dict[str, Any]]) -> None:
    """Persist processed clip metadata to disk."""
    _ensure_dirs()
    _atomic_json_write(PROCESSED_CLIPS_FILE, clips)


class ClipFetcher:
    """Twitch API + yt-dlp facade used by the Streamlit UI."""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self.client_id = (client_id or os.getenv("TWITCH_CLIENT_ID") or "").strip()
        self.client_secret = (client_secret or os.getenv("TWITCH_CLIENT_SECRET") or "").strip()
        self._token: str | None = None

    @property
    def credentials_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def get_twitch_token(self) -> str | None:
        """Obtain an app access token via the OAuth2 client-credentials flow."""
        if not self.credentials_configured:
            logger.warning("Twitch credentials are not configured; API fetching disabled.")
            return None
        if self._token:
            return self._token
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        try:
            response = requests.post(TWITCH_OAUTH_URL, data=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            self._token = str(response.json()["access_token"])
            return self._token
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.error("Failed to obtain Twitch access token: %s", exc)
            return None

    def _headers(self) -> dict[str, str] | None:
        token = self.get_twitch_token()
        if not token:
            return None
        return {"Authorization": f"Bearer {token}", "Client-ID": self.client_id}

    def _find_game_id(self, game_name: str, headers: dict[str, str]) -> str | None:
        url = f"{TWITCH_API_BASE}/games?name={quote(game_name)}"
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            games = response.json().get("data", [])
        except (requests.RequestException, ValueError) as exc:
            logger.error("Twitch game lookup failed: %s", exc)
            return None
        if not games:
            logger.warning("Twitch game not found: %s", game_name)
            return None
        return str(games[0]["id"])

    def fetch_top_clips(
        self, game_name: str = DEFAULT_GAME, first: int = 10, period: str = "7d"
    ) -> list[dict[str, Any]]:
        """Return normalized metadata for the most popular clips of a game."""
        headers = self._headers()
        if not headers:
            return []
        game_id = self._find_game_id(game_name, headers)
        if not game_id:
            return []
        safe_first = max(1, min(int(first), 100))
        safe_period = period if period in {"day", "week", "month", "all", "24h", "7d", "30d"} else "7d"
        if safe_period == "24h":
            safe_period = "day"
        elif safe_period == "7d":
            safe_period = "week"
        elif safe_period == "30d":
            safe_period = "month"
        url = (
            f"{TWITCH_API_BASE}/clips?game_id={quote(game_id)}"
            f"&first={safe_first}&period={safe_period}&is_relevant=True"
        )
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            clips = response.json().get("data", [])
        except (requests.RequestException, ValueError) as exc:
            logger.error("Twitch clip fetch failed: %s", exc)
            return []
        normalized = [
            _normalize_twitch_clip(clip, game_name)
            for clip in clips
            if isinstance(clip, dict) and clip.get("id")
        ]
        logger.info("Fetched %d Twitch clips for %s", len(normalized), game_name)
        return normalized

    def load_processed(self) -> list[dict[str, Any]]:
        return load_processed_clips()

    def save_processed(self, clips: list[dict[str, Any]]) -> None:
        save_processed_clips(clips)

    def is_processed(self, clip_id: str) -> bool:
        target = str(clip_id)
        return any(str(item.get("id")) == target for item in load_processed_clips())

    def mark_processed(self, clip_id: str, metadata: dict[str, Any]) -> None:
        """Insert or update a clip record in the processed index."""
        records = load_processed_clips()
        record = dict(metadata)
        record["id"] = str(clip_id)
        record.setdefault("processed_at", _iso_now())
        for index, item in enumerate(records):
            if str(item.get("id")) == str(clip_id):
                records[index] = {**item, **record}
                break
        else:
            records.append(record)
        save_processed_clips(records)

    def _extract_info(self, url: str) -> dict[str, Any] | None:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": REQUEST_TIMEOUT,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            return info if isinstance(info, dict) else None
        except (yt_dlp.utils.YoutubeDLError, OSError) as exc:
            logger.error("yt-dlp metadata extraction failed for %s: %s", url, exc)
            return None

    def download_clip(
        self, url: str, output_dir: Path, clip_id: str | None = None
    ) -> Path | None:
        """Download a Twitch/YouTube clip and return the local video path."""
        if not url or not str(url).strip():
            logger.error("Cannot download an empty URL.")
            return None
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        resolved_id = _safe_clip_id(clip_id) if clip_id else ""
        if not resolved_id:
            info = self._extract_info(url)
            if info and info.get("id"):
                resolved_id = _safe_clip_id(str(info["id"]))
            else:
                resolved_id = _guess_clip_id_from_url(url)

        target = output_dir / f"{resolved_id}.mp4"
        if target.exists() and target.stat().st_size > 0:
            logger.info("Clip already downloaded: %s", target)
            return target

        options = {
            "outtmpl": str(output_dir / f"{resolved_id}.%(ext)s"),
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "overwrites": False,
            "continuedl": True,
            "retries": 3,
            "socket_timeout": REQUEST_TIMEOUT,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
        except (yt_dlp.utils.YoutubeDLError, OSError) as exc:
            logger.error("yt-dlp download failed for %s: %s", url, exc)
            return None

        candidates = [
            path
            for path in output_dir.glob(f"{resolved_id}.*")
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and path.stat().st_size > 0
        ]
        if not candidates:
            logger.error("yt-dlp produced no video file for %s", url)
            return None
        preferred = next((path for path in candidates if path.suffix.lower() == ".mp4"), candidates[0])
        logger.info("Downloaded %s -> %s", url, preferred)
        return preferred

    def process_clip(
        self, clip_data: dict[str, Any], raw_dir: Path, force: bool = False
    ) -> dict[str, Any]:
        """Download one clip unless it has already been processed."""
        clip_id = str(clip_data.get("id") or "").strip()
        if not clip_id:
            return {
                "ok": False,
                "path": None,
                "skipped": False,
                "error": "Clip metadata is missing an id.",
                "clip_id": "",
            }

        if self.is_processed(clip_id) and not force:
            return {"ok": True, "path": None, "skipped": True, "error": None, "clip_id": clip_id}

        url = str(clip_data.get("url") or "").strip()
        if not url:
            return {
                "ok": False,
                "path": None,
                "skipped": False,
                "error": "Clip metadata is missing a URL.",
                "clip_id": clip_id,
            }

        path = self.download_clip(url, raw_dir, clip_id)
        if not path:
            return {
                "ok": False,
                "path": None,
                "skipped": False,
                "error": "yt-dlp could not download this clip.",
                "clip_id": clip_id,
            }

        metadata = {**clip_data, "id": clip_id, "raw_path": str(path)}
        metadata.setdefault("processed_at", _iso_now())
        self.mark_processed(clip_id, metadata)
        return {"ok": True, "path": path, "skipped": False, "error": None, "clip_id": clip_id}


def fetch_twitch_clips(
    game_name: str = DEFAULT_GAME, first: int = 10, period: str = "7d"
) -> list[dict[str, Any]]:
    """Convenience wrapper used by the UI."""
    return ClipFetcher().fetch_top_clips(game_name=game_name, first=first, period=period)


def download_clip_with_ytdlp(url: str, output_dir: Path) -> Path | None:
    """Convenience wrapper used by the UI."""
    return ClipFetcher().download_clip(url, Path(output_dir))
