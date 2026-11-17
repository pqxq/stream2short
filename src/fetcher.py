"""Clip discovery and download helpers.

The fetcher resolves a game dynamically through the Twitch Helix ``/games``
endpoint and paginates ``/helix/clips`` in order to filter by language on the
client side.  Twitch does not expose a ``language`` query parameter for clips,
so page size is fixed at 100 and up to ten pages are scanned per request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml
import yt_dlp

from src.languages import normalize_language

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
PROCESSED_CLIPS_FILE = DATA_DIR / "processed_clips.json"

TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"

DEFAULT_GAME = "Grand Theft Auto VI"
VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".flv")
REQUEST_TIMEOUT = 30
MAX_CLIP_PAGES = 10
PAGE_SIZE = 100
MAX_REQUEST_ATTEMPTS = 3


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


def load_config() -> dict[str, Any]:
    """Load config.yaml with a minimal safe fallback."""
    config: dict[str, Any] = {"default_game": DEFAULT_GAME, "default_language": "en"}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not read %s: %s", CONFIG_PATH, exc)
    return config


def get_default_game() -> str:
    """Return the configured default game name."""
    game = str(load_config().get("default_game") or DEFAULT_GAME).strip()
    return game or DEFAULT_GAME


def _normalize_period(period: str) -> str:
    mapping = {
        "24h": "day",
        "day": "day",
        "7d": "week",
        "week": "week",
        "30d": "month",
        "month": "month",
        "all": "all",
    }
    return mapping.get(str(period).strip().lower(), "week")


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
        "language": str(clip.get("language") or "").strip().lower(),
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

    def _twitch_get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """GET a Helix endpoint with bounded retries and one token refresh."""
        refreshed = False
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            headers = self._headers()
            if not headers:
                return None
            url = f"{TWITCH_API_BASE}{path}"
            try:
                response = requests.get(
                    url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Twitch request %s failed (attempt %d/%d): %s",
                    path,
                    attempt,
                    MAX_REQUEST_ATTEMPTS,
                    exc,
                )
                if attempt == MAX_REQUEST_ATTEMPTS:
                    return None
                time.sleep(min(2**attempt, 8))
                continue

            if response.status_code in {401, 403} and not refreshed:
                logger.info("Twitch token rejected; refreshing once.")
                self._token = None
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else float(2**attempt)
                except (TypeError, ValueError):
                    wait_seconds = float(2**attempt)
                logger.warning(
                    "Twitch HTTP %s on %s (attempt %d/%d); retrying in %.1fs",
                    response.status_code,
                    path,
                    attempt,
                    MAX_REQUEST_ATTEMPTS,
                    wait_seconds,
                )
                if attempt == MAX_REQUEST_ATTEMPTS:
                    return None
                time.sleep(min(wait_seconds, 15.0))
                continue

            try:
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.error("Twitch request %s failed: %s", path, exc)
                return None
            return payload if isinstance(payload, dict) else None
        return None

    @staticmethod
    def _pick_game_entry(entries: Any, query: str) -> dict[str, str] | None:
        if not isinstance(entries, list):
            return None
        games = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            game_id = str(entry.get("id") or "").strip()
            if not game_id:
                continue
            games.append(
                {"id": game_id, "name": str(entry.get("name") or "").strip()}
            )
        if not games:
            return None
        exact = next(
            (
                game
                for game in games
                if game["name"].casefold() == query.strip().casefold()
            ),
            None,
        )
        return exact or games[0]

    @staticmethod
    def _game_search_terms(game_name: str) -> list[str]:
        terms = [game_name]
        match = re.match(r"^(?P<base>.+?)\s+\d+(?:\.\d+)*$", game_name)
        if match:
            base = match.group("base").strip()
            if base and base.casefold() != game_name.casefold():
                terms.append(base)
        return terms

    def _find_game(self, game_name: str) -> dict[str, str] | None:
        """Resolve a game name to its official Twitch id and canonical name."""
        query = str(game_name or "").strip()
        if not query:
            return None

        payload = self._twitch_get("/games", {"name": query})
        if payload is not None:
            selected = self._pick_game_entry(payload.get("data"), query)
            if selected:
                return selected

        for term in self._game_search_terms(query):
            payload = self._twitch_get("/search/categories", {"query": term})
            if payload is None:
                continue
            selected = self._pick_game_entry(payload.get("data"), term)
            if selected:
                logger.info(
                    "Resolved Twitch game %s to %s via category search",
                    query,
                    selected["name"] or selected["id"],
                )
                return selected

        logger.warning("Twitch game not found: %s", query)
        return None

    def _find_game_id(self, game_name: str) -> str | None:
        game = self._find_game(game_name)
        return game["id"] if game else None

    def fetch_top_clips(
        self,
        game_name: str | None = None,
        first: int = 10,
        period: str = "7d",
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return normalized metadata for popular clips, optionally by language."""
        resolved_game = (game_name or get_default_game()).strip() or get_default_game()
        if not self._headers():
            return []
        game = self._find_game(resolved_game)
        if not game:
            return []
        game_id = game["id"]
        canonical_game = game["name"] or resolved_game

        safe_first = max(1, min(int(first), 100))
        safe_period = _normalize_period(period)
        target_language = normalize_language(language)

        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for page in range(MAX_CLIP_PAGES):
            params: dict[str, Any] = {
                "game_id": game_id,
                "first": PAGE_SIZE,
                "period": safe_period,
                "is_relevant": "true",
            }
            if cursor:
                params["after"] = cursor
            payload = self._twitch_get("/clips", params)
            if payload is None:
                break

            clips = payload.get("data", [])
            if not isinstance(clips, list) or not clips:
                break

            for clip in clips:
                if not isinstance(clip, dict) or not clip.get("id"):
                    continue
                normalized = _normalize_twitch_clip(clip, canonical_game)
                if target_language is not None and normalized["language"] != target_language:
                    continue
                collected.append(normalized)
                if len(collected) >= safe_first:
                    break

            if len(collected) >= safe_first:
                break

            next_cursor = payload.get("pagination", {}).get("cursor")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = str(next_cursor)
            logger.debug(
                "Fetched page %d for %s; %d matching clips so far",
                page + 1,
                canonical_game,
                len(collected),
            )

        result = collected[:safe_first]
        if target_language and len(result) < safe_first:
            logger.info(
                "Only %d/%d clips matched language '%s' after scanning up to %d pages.",
                len(result),
                safe_first,
                target_language,
                MAX_CLIP_PAGES,
            )
        logger.info(
            "Fetched %d Twitch clips for %s (language=%s)",
            len(result),
            canonical_game,
            target_language or "all",
        )
        return result

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
        metadata.setdefault("language", normalize_language(clip_data.get("language")) or "")
        metadata.setdefault("processed_at", _iso_now())
        self.mark_processed(clip_id, metadata)
        return {"ok": True, "path": path, "skipped": False, "error": None, "clip_id": clip_id}


def fetch_twitch_clips(
    game_name: str | None = None,
    first: int = 10,
    period: str = "7d",
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper used by the UI."""
    return ClipFetcher().fetch_top_clips(
        game_name=game_name, first=first, period=period, language=language
    )


def download_clip_with_ytdlp(url: str, output_dir: Path) -> Path | None:
    """Convenience wrapper used by the UI."""
    return ClipFetcher().download_clip(url, Path(output_dir))
