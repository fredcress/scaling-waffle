"""Runtime configuration, all from environment variables."""
import os


def _list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in os.environ.get(name, default).split(",") if p.strip()]


PHOTOS_DIR = os.environ.get("PHOTOS_DIR", "/photos")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
USERS = _list("USERS", "Husband,Wife")
# Glob patterns matched against the path relative to PHOTOS_DIR, e.g. "Uncle Bob/*"
EXCLUDE = _list("EXCLUDE")
WORKERS = max(1, int(os.environ.get("WORKERS", "2")))
VIDEO_PREVIEWS = os.environ.get("VIDEO_PREVIEWS", "1") not in ("0", "false", "no", "")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

IMMICH_URL = os.environ.get("IMMICH_URL", "").rstrip("/")
IMMICH_API_KEY = os.environ.get("IMMICH_API_KEY", "")
IMMICH_ALBUM = os.environ.get("IMMICH_ALBUM", "Wedding - guest picks")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "")

# "Possibly junk" thresholds (photos are only flagged, never hidden or deleted).
BLUR_BELOW = float(os.environ.get("BLUR_BELOW", "45"))      # sharpness score under this = blurry
DARK_BELOW = float(os.environ.get("DARK_BELOW", "28"))      # mean brightness 0-255
BRIGHT_ABOVE = float(os.environ.get("BRIGHT_ABOVE", "235"))
FLAT_BELOW = float(os.environ.get("FLAT_BELOW", "12"))      # contrast under this = no detail at all
# Burst = same guest, shots at most this many seconds apart that look alike.
BURST_SECONDS = float(os.environ.get("BURST_SECONDS", "10"))
BURST_DISTANCE = int(os.environ.get("BURST_DISTANCE", "12"))  # 0 = identical, 64 = unrelated

DB_PATH = os.path.join(DATA_DIR, "triage.sqlite3")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
