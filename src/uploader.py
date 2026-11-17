import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.oauth2 import exceptions as oauth_exceptions
from google.oauth2.credentials import Credentials
from googleapiclient import errors as gapi_errors
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
DATA_DIR = Path("data")
TOKEN_EN_FILE = DATA_DIR / "token_en.json"
TOKEN_RU_FILE = DATA_DIR / "token_ru.json"
QUOTA_MAX_DAILY_UPLOADS = 5

def get_credentials_from_token(token_file: Path) -> Credentials | None:
    if not token_file.exists():
        logger.error("Token file not found: %s", token_file)
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_file.read_text()), scopes=SCOPES)
        return creds
    except (oauth_exceptions.OAuthError, ValueError, KeyError, json.JSONDecodeError) as e:
        logger.error("Failed to load credentials from %s: %s", token_file, e)
        return None

def refresh_credentials_if_needed(creds: Credentials) -> Credentials:
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(token=creds.refresh_token)
            logger.info("Credentials refreshed")
        except oauth_exceptions.OAuthError as e:
            logger.error("Failed to refresh credentials: %s", e)
            raise
    return creds

def get_youtube_service(token_file: Path) -> Any | None:
    creds = get_credentials_from_token(token_file)
    if not creds:
        return None
    creds = refresh_credentials_if_needed(creds)
    try:
        service = build('youtube', 'v3', credentials=creds)
        return service
    except gapi_errors.HttpError as e:
        logger.error("Failed to build YouTube service: %s", e)
        return None

def check_daily_quota(service: Any, token_file: Path) -> bool:
    today = datetime.now(UTC).date().isoformat()
    # List recent uploads from the channel to count today's uploads (simplify: last 50 videos)
    try:
        channel_response = service.channels().list(
            part='contentDetails',
            mine=True
        ).execute()
        uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        playlist_items = service.playlistItems().list(
            playlistId=uploads_playlist_id,
            part='snippet',
            maxResults=50
        ).execute().get('items', [])
        # Count videos uploaded today
        count = 0
        for item in playlist_items:
            video_published_at = item['snippet']['publishedAt']
            video_date = video_published_at.split('T')[0]
            if video_date == today:
                count += 1
                if count >= QUOTA_MAX_DAILY_UPLOADS:
                    logger.warning("Daily quota reached: %d uploads today", count)
                    return False
        return True
    except (gapi_errors.HttpError, KeyError) as e:
        logger.error("Failed to check daily quota: %s", e)
        return True  # assume quota ok on error

def schedule_video(
    token_file: Path,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    category: int = 22,  # People & Blogs
    privacy: str = 'public',
    start_time: datetime | None = None,
    interval_hours: int = 24
) -> str | None:
    """Schedule a video upload.
    If start_time is None, schedule at next interval from now.
    """
    service = get_youtube_service(token_file)
    if not service:
        logger.error("YouTube service unavailable for %s", token_file.name)
        return None
    # Check quota
    if not check_daily_quota(service, token_file):
        logger.error("Quota exceeded for %s", token_file.name)
        return None

    # Determine publishAt
    if start_time is None:
        # Find next interval from now
        now = datetime.now(UTC)
        # Align to hour boundaries? Just use now + 1 hour as next slot
        start_time = now + timedelta(hours=1)
    publish_at = start_time.isoformat().replace('+00:00', 'Z')

    # Prepare request
    request = service.videos().insert(
        part='snippet,status',
        body={
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': str(category),
                'defaultLanguage': 'en',
                'defaultAudioLanguage': 'en',
            },
            'status': {
                'privacyStatus': privacy,
                'publishAt': publish_at,
            }
        },
        media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype='video/*')
    )
    try:
        response = request.execute()
        video_id = response['id']
        logger.info("Scheduled video ID %s for %s", video_id, publish_at)
        return video_id
    except gapi_errors.HttpError as e:
        logger.error("Failed to schedule video: %s", e)
        return None

class YouTubeUploader:
    def __init__(self):
        self.quotas = {
            "en": QUOTA_MAX_DAILY_UPLOADS,
            "ru": QUOTA_MAX_DAILY_UPLOADS,
        }
        self.upload_counts = {"en": 0, "ru": 0}
        self.load_counts_from_state()

    def load_counts_from_state(self):
        # Could load from a file, but for simplicity track in memory
        # In production, you would persist daily counts.
        pass

    def upload_and_schedule(
        self,
        video_path: Path,
        hook_text: str,
        title: str,
        description: str,
        language: str = "en",
        start_time: datetime | None = None,
        interval_hours: int = 24
    ) -> bool:
        if language == "en":
            token_file = TOKEN_EN_FILE
        else:
            token_file = TOKEN_RU_FILE
        if not token_file.exists():
            logger.error("Token file not found: %s", token_file)
            return False
        # Generate a more detailed description with hook
        full_description = f"{description}\n\nHook: {hook_text}"
        tags = []
        if language == "en":
            tags = ["gaming", "GTA6", "shorts", "viral"]
        else:
            tags = ["gaming", "GTA6", "shorts", "russian", "вирусный"]
        video_id = schedule_video(
            token_file,
            video_path,
            title,
            full_description,
            tags,
            start_time=start_time,
            interval_hours=interval_hours
        )
        if video_id:
            logger.info("Uploaded and scheduled %s video: %s", language, video_id)
            return True
        else:
            logger.error("Failed to schedule %s video", language)
            return False

    def check_quota_status(self) -> dict[str, Any]:
        status = {}
        for lang in ["en", "ru"]:
            status[lang] = {
                "max_daily": self.quotas[lang],
                "used_today": self.upload_counts[lang],
                "remaining": self.quotas[lang] - self.upload_counts[lang],
                "ok": self.upload_counts[lang] < self.quotas[lang]
            }
        return status

if __name__ == "__main__":
    # Simple CLI test: schedule a video if token file exists
    import sys
    if len(sys.argv) < 2:
        logger.info("Usage: python src/uploader.py <video_path>")
        sys.exit(1)
    video_path = Path(sys.argv[1])
    if not video_path.exists():
        logger.error("Video file not found: %s", video_path)
        sys.exit(1)
    # Use existing token file if present
    token_file = TOKEN_EN_FILE if TOKEN_EN_FILE.exists() else TOKEN_RU_FILE
    if not token_file.exists():
        logger.error("No token file found")
        sys.exit(1)
    video_id = schedule_video(
        token_file,
        video_path,
        "Test Upload via CLI",
        "Test upload from GTA6 Clip Factory uploader module.",
        ["test", "upload"]
    )
    if video_id:
        logger.info("Test upload scheduled with ID: %s", video_id)
    else:
        logger.error("Test upload failed.")
