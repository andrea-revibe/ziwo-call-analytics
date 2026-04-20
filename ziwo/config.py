import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
AUDIO_DIR = DATA_DIR / "audio"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "calls.db"

BASE_URL = os.environ.get("ZIWO_BASE_URL", "").rstrip("/")
USERNAME = os.environ.get("ZIWO_USERNAME", "")
PASSWORD = os.environ.get("ZIWO_PASSWORD", "")
TARGET_DATE = os.environ.get("ZIWO_TARGET_DATE", "2026-04-10")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

MYSQL_HOST = os.environ.get("MYSQL_HOST", "")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "")


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
