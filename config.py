"""Central configuration — every env var the app uses is loaded here."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# Database — Render hands out postgres:// URLs; SQLAlchemy 2 wants postgresql://
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'dev.db'}"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# App — on Render, RENDER_EXTERNAL_URL is set automatically on web services
APP_BASE_URL = (os.getenv("APP_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
                or "http://localhost:5000").rstrip("/")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-only-not-secret")
GENERATE_SECRET = os.getenv("GENERATE_SECRET", "")

# Dashboard logins — one password per person, all through the same login box.
# DASHBOARD_PASSWORD (Dr. Ike) and ERICA_PASSWORD (his publicist) are client
# logins; ADMIN_PASSWORD (Vernon) additionally sees the Needs-attention strip
# and system-health warnings. An empty value disables that login.
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
ERICA_PASSWORD = os.getenv("ERICA_PASSWORD", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Publishing (all platforms)
USE_MOCK_PUBLISHER = _bool("USE_MOCK_PUBLISHER", True)

# Instagram — Meta retires Graph API versions after ~2 years; bump the version
# here when wiring credentials (an expired version fails with an error message)
IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
GRAPH_API_BASE = os.getenv("GRAPH_API_BASE", "https://graph.facebook.com/v23.0")

# LinkedIn
LINKEDIN_ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
LINKEDIN_AUTHOR_URN = os.getenv("LINKEDIN_AUTHOR_URN", "")  # e.g. urn:li:person:xxxx
LINKEDIN_API_BASE = os.getenv("LINKEDIN_API_BASE", "https://api.linkedin.com/rest")
LINKEDIN_VERSION = os.getenv("LINKEDIN_VERSION", "202606")
# LinkedIn tokens can't report their own expiry — set this to the date shown
# when the token was authorized so the dashboard can warn before it lapses
try:
    LINKEDIN_TOKEN_EXPIRES = date.fromisoformat(
        os.getenv("LINKEDIN_TOKEN_EXPIRES", "2026-09-11").strip())
except ValueError:
    LINKEDIN_TOKEN_EXPIRES = None

# Library photos published this long ago quietly return to the pool
PHOTO_REUSE_DAYS = int(os.getenv("PHOTO_REUSE_DAYS", "60"))

# Instagram token refresh (refresh_token.py only) — the IG Graph API runs on
# a Meta developer app, so the app credentials stay even without Facebook posting
FB_APP_ID = os.getenv("FB_APP_ID", "")
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")

# Paths
MEDIA_DIR = BASE_DIR / "static" / "media"
# optional: every dashboard photo upload is also copied here, untouched
_backup = os.getenv("PHOTOS_BACKUP_DIR", "").strip()
PHOTOS_BACKUP_DIR = Path(_backup) if _backup else None
FONT_DIR = BASE_DIR / "static" / "fonts"
SAMPLE_POSTS_FILE = BASE_DIR / "sample_posts.txt"       # his real LinkedIn posts
BOOK_QUOTES_FILE = BASE_DIR / "book_quotes.txt"         # signature lines from his book
