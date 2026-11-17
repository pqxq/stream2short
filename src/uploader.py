"""YouTube Data API v3 uploader with per-language channels and quota guards.

Each language niche uses its own OAuth2 token file under ``data/`` named
``token_<lang_code>.json`` (for example ``token_en.json`` or ``token_uk.json``).
Uploads are spaced with ISO-8601 ``publishAt`` timestamps and a local ledger
keeps a hard cap of five uploads per language channel per UTC day.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from src.languages import normalize_language, validate_language_code

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CLIENT_SECRETS_FILE = DATA_DIR / "client_secret.json"
UPLOAD_HISTORY_FILE = DATA_DIR / "upload_history.json"
TOKEN_GLOB = "token_*.json"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
QUOTA_MAX_DAILY_UPLOADS = 5
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 5000
MIN_SCHEDULE_LEAD = timedelta(minutes=15)
MAX_SCHEDULE_INTERVAL_HOURS = 168
UPLOAD_RETRIES = 3

_TOKEN_PATTERN = re.compile(r"^token_([a-z]{2,3})[.]json$")


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return default


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dirs()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temp_path.replace(path)


def get_token_path(language: str) -> Path:
    """Return the canonical token path for a language code."""
    return DATA_DIR / f"token_{validate_language_code(language)}.json"


def token_language(token_file: str | Path) -> str:
    """Extract a language code from a ``token_<lang>.json`` path."""
    name = Path(token_file).name
    match = _TOKEN_PATTERN.fullmatch(name)
    if not match:
        raise ValueError(f"Token filename does not contain a language code: {name}")
    return validate_language_code(match.group(1))


def list_connected_languages() -> list[str]:
    """Return every language with a token file, sorted alphabetically."""
    languages: set[str] = set()
    for path in DATA_DIR.glob(TOKEN_GLOB):
        match = _TOKEN_PATTERN.fullmatch(path.name)
        if match:
            code = normalize_language(match.group(1))
            if code:
                languages.add(code)
    return sorted(languages)


def _normalize_language_list(languages: list[str] | None) -> list[str]:
    codes: list[str] = []
    for item in languages or []:
        code = normalize_language(item)
        if code and code not in codes:
            codes.append(code)
    return codes


def _load_history() -> dict[str, list[dict[str, Any]]]:
    payload = _read_json(UPLOAD_HISTORY_FILE, {})
    history: dict[str, list[dict[str, Any]]] = {}
    if isinstance(payload, dict):
        for channel, entries in payload.items():
            code = normalize_language(channel)
            if code and isinstance(entries, list):
                history[code] = [entry for entry in entries if isinstance(entry, dict)]
    return history


def _save_history(history: dict[str, list[dict[str, Any]]]) -> None:
    _write_json(UPLOAD_HISTORY_FILE, history)


def _today_count(language: str) -> int:
    code = validate_language_code(language)
    today = datetime.now(UTC).date()
    count = 0
    for entry in _load_history().get(code, []):
        raw_timestamp = entry.get("uploaded_at")
        if not isinstance(raw_timestamp, str):
            continue
        try:
            normalized = raw_timestamp[:-1] + "+00:00" if raw_timestamp.endswith("Z") else raw_timestamp
            uploaded_at = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if uploaded_at.tzinfo is None:
            uploaded_at = uploaded_at.replace(tzinfo=UTC)
        if uploaded_at.astimezone(UTC).date() == today:
            count += 1
    return count


def _record_upload(language: str, video_id: str, publish_at: datetime) -> None:
    code = validate_language_code(language)
    history = _load_history()
    history.setdefault(code, []).append(
        {
            "video_id": video_id,
            "publish_at": publish_at.isoformat(),
            "uploaded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
    )
    _save_history(history)


def get_credentials_from_token(token_file: Path) -> Credentials | None:
    """Load OAuth2 credentials from a token JSON file."""
    token_file = Path(token_file)
    if not token_file.exists():
        logger.error("Token file not found: %s", token_file)
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except (OSError, ValueError, KeyError, GoogleAuthError) as exc:
        logger.error("Failed to load credentials from %s: %s", token_file, exc)
        return None


def refresh_credentials_if_needed(
    creds: Credentials, token_file: Path | None = None
) -> Credentials:
    """Refresh expired credentials and persist the new token when possible."""
    if not creds.expired:
        return creds
    if not creds.refresh_token:
        raise RefreshError("Credentials are expired and have no refresh token.")
    try:
        creds.refresh(Request())
    except (RefreshError, GoogleAuthError) as exc:
        logger.error("Failed to refresh Google credentials: %s", exc)
        raise
    if token_file is not None and creds.valid:
        Path(token_file).write_text(creds.to_json(), encoding="utf-8")
        logger.info("Refreshed credentials saved to %s", token_file)
    return creds


def get_youtube_service(token_file: Path) -> Any | None:
    """Build an authorised YouTube Data API client or return None."""
    creds = get_credentials_from_token(token_file)
    if not creds:
        return None
    try:
        creds = refresh_credentials_if_needed(creds, Path(token_file))
        return build("youtube", "v3", credentials=creds, cache_discovery=False)
    except (RefreshError, GoogleAuthError, HttpError, OSError) as exc:
        logger.error("Failed to build YouTube service for %s: %s", token_file, exc)
        return None


def get_token_status(token_file: Path) -> dict[str, Any]:
    """Return connection information without performing a network call."""
    token_file = Path(token_file)
    status: dict[str, Any] = {
        "token_file": str(token_file),
        "exists": token_file.exists(),
        "valid": False,
        "expired": None,
        "has_refresh_token": False,
    }
    if not token_file.exists():
        return status
    creds = get_credentials_from_token(token_file)
    if creds is None:
        return status
    status.update(
        {
            "valid": bool(creds.valid),
            "expired": bool(creds.expired),
            "has_refresh_token": bool(creds.refresh_token),
        }
    )
    return status


def check_daily_quota(language_or_token: str | Path) -> bool:
    """Return True when another upload is allowed for this channel today."""
    if isinstance(language_or_token, Path):
        code = token_language(language_or_token)
    else:
        code = validate_language_code(language_or_token)
    return _today_count(code) < QUOTA_MAX_DAILY_UPLOADS


def authorize_channel(language: str, client_secrets_file: Path | None = None) -> Path:
    """Run the installed-app OAuth flow and save a token for one language."""
    code = validate_language_code(language)
    secrets_path = Path(client_secrets_file) if client_secrets_file else CLIENT_SECRETS_FILE
    if not secrets_path.exists():
        raise FileNotFoundError(
            f"Google OAuth client secrets not found: {secrets_path}. "
            "Download it from Google Cloud Console and save it as data/client_secret.json."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_file = get_token_path(code)
    _ensure_dirs()
    token_file.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Saved %s channel token to %s", code.upper(), token_file)
    return token_file


def _validate_metadata(title: str, description: str, tags: list[str] | None) -> list[str]:
    if not title.strip():
        raise ValueError("YouTube title must not be empty.")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"YouTube title must be at most {MAX_TITLE_LENGTH} characters.")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"YouTube description must be at most {MAX_DESCRIPTION_LENGTH} characters."
        )
    cleaned_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    if sum(len(tag) for tag in cleaned_tags) > 450:
        cleaned_tags = cleaned_tags[:15]
    return cleaned_tags


def schedule_video(
    token_file: Path,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str] | None = None,
    publish_at: datetime | None = None,
    category: int = 22,
    privacy: str = "public",
    language: str | None = None,
) -> str | None:
    """Upload a video with ``publishAt`` and return its YouTube video id."""
    token_file = Path(token_file)
    video_path = Path(video_path)
    code = validate_language_code(language) if language else token_language(token_file)

    if not video_path.exists() or video_path.stat().st_size == 0:
        logger.error("Video file is missing or empty: %s", video_path)
        return None
    try:
        cleaned_tags = _validate_metadata(title, description, tags)
    except ValueError as exc:
        logger.error("Invalid YouTube metadata: %s", exc)
        return None
    if _today_count(code) >= QUOTA_MAX_DAILY_UPLOADS:
        logger.error("Daily upload quota reached for %s channel.", code.upper())
        return None

    service = get_youtube_service(token_file)
    if not service:
        return None

    now = datetime.now(UTC)
    if publish_at is None:
        publish_at = now + timedelta(hours=1)
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=UTC)
    publish_at = publish_at.astimezone(UTC)
    publish_at = max(publish_at, now + MIN_SCHEDULE_LEAD)

    body = {
        "snippet": {
            "title": title.strip(),
            "description": description.strip(),
            "tags": cleaned_tags,
            "categoryId": str(category),
            "defaultLanguage": code,
            "defaultAudioLanguage": code,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at.isoformat().replace("+00:00", "Z"),
            "selfDeclaredMadeForKids": False,
            "license": "youtube",
            "embeddable": True,
        },
    }
    if privacy == "unlisted" and publish_at <= now + MIN_SCHEDULE_LEAD:
        body["status"]["privacyStatus"] = "unlisted"

    media = MediaFileUpload(
        str(video_path),
        chunksize=8 * 1024 * 1024,
        resumable=True,
        mimetype="video/*",
    )
    request = service.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    video_id: str | None = None
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            response = None
            while response is None:
                _status, response = request.next_chunk()
            video_id = str(response.get("id") or "") or None
            break
        except HttpError as exc:
            status_code = getattr(exc.resp, "status", 0)
            logger.warning(
                "YouTube upload attempt %d/%d failed with HTTP %s: %s",
                attempt,
                UPLOAD_RETRIES,
                status_code,
                exc,
            )
            retryable = status_code in {429, 500, 502, 503, 504}
            if not retryable or attempt == UPLOAD_RETRIES:
                return None
            time.sleep(2**attempt)
        except (RefreshError, GoogleAuthError, OSError) as exc:
            logger.error("YouTube upload failed: %s", exc)
            return None

    if not video_id:
        logger.error("YouTube upload finished without a video id.")
        return None
    _record_upload(code, video_id, publish_at)
    logger.info(
        "Scheduled %s video %s for %s", code.upper(), video_id, publish_at.isoformat()
    )
    return video_id


class YouTubeUploader:
    """High-level per-language upload facade used by the Streamlit UI."""

    def __init__(self, max_daily: int = QUOTA_MAX_DAILY_UPLOADS) -> None:
        self.max_daily = max_daily

    @property
    def token_files(self) -> dict[str, Path]:
        return {code: get_token_path(code) for code in list_connected_languages()}

    def token_status(self, languages: list[str] | None = None) -> dict[str, dict[str, Any]]:
        codes = _normalize_language_list(languages)
        for code in list_connected_languages():
            if code not in codes:
                codes.append(code)
        return {code: get_token_status(get_token_path(code)) for code in codes}

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        language: str = "en",
        tags: list[str] | None = None,
        start_time: datetime | None = None,
        interval_hours: int = 24,
        slot_index: int = 0,
    ) -> bool:
        """Schedule one language-specific video and respect the daily quota."""
        code = validate_language_code(language)
        token_file = get_token_path(code)
        if not token_file.exists():
            logger.error("No OAuth token connected for %s channel.", code.upper())
            return False

        interval = max(1, min(int(interval_hours), MAX_SCHEDULE_INTERVAL_HOURS))
        slot = max(0, int(slot_index))
        if start_time is None:
            publish_at = datetime.now(UTC) + timedelta(hours=1 + interval * slot)
        else:
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            publish_at = start_time + timedelta(hours=interval * slot)

        default_tags = ["gaming", "shorts", "viral", code]
        video_id = schedule_video(
            token_file=token_file,
            video_path=Path(video_path),
            title=title,
            description=description,
            tags=tags or default_tags,
            publish_at=publish_at,
            privacy="public",
            language=code,
        )
        return bool(video_id)

    def check_quota_status(
        self, languages: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Return per-channel quota usage for the Settings tab."""
        codes = _normalize_language_list(languages)
        for code in list_connected_languages():
            if code not in codes:
                codes.append(code)
        history = _load_history()
        status: dict[str, dict[str, Any]] = {}
        for code in codes:
            token_file = get_token_path(code)
            used = _today_count(code)
            status[code] = {
                "token_file": str(token_file),
                "connected": token_file.exists(),
                "max_daily": self.max_daily,
                "used_today": used,
                "remaining": max(0, self.max_daily - used),
                "ok": used < self.max_daily,
                "recent": history.get(code, [])[-5:],
            }
        return status


def upload_clip(
    video_path: Path,
    title: str,
    description: str,
    language: str = "en",
    start_time: datetime | None = None,
    interval_hours: int = 24,
    slot_index: int = 0,
) -> bool:
    """Convenience wrapper used by the UI."""
    return YouTubeUploader().upload(
        video_path,
        title,
        description,
        language=language,
        start_time=start_time,
        interval_hours=interval_hours,
        slot_index=slot_index,
    )
