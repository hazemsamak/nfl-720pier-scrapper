import os
import re
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load .env file from current directory or parent directories
load_dotenv()


def normalize_schedule_time(val: str) -> Optional[str]:
    """
    Normalizes time strings like '09:00', '9:00', '9 AM', '3 PM', '9 PM', '15:00' to standard 'HH:MM'.
    """
    cleaned = val.strip().lower()
    if not cleaned:
        return None
    # 12-hour format: e.g. "9 am", "9:30 am", "3 pm", "9 pm", "12 pm"
    m12 = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", cleaned)
    if m12:
        h = int(m12.group(1))
        m = int(m12.group(2) or 0)
        meridiem = m12.group(3)
        if 1 <= h <= 12 and 0 <= m < 60:
            if meridiem == "pm" and h != 12:
                h += 12
            elif meridiem == "am" and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        return None
    # 24-hour format: e.g. "09:00", "9:00", "15:00", "21:00"
    m24 = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", cleaned)
    if m24:
        h = int(m24.group(1))
        m = int(m24.group(2))
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    return None


@dataclass
class Config:
    # 720pier Tracker
    pier_thread_url: str
    pier_username: Optional[str]
    pier_password: Optional[str]
    pier_cookie: Optional[str]
    quality_preference: Optional[str]
    favorite_teams: list[str]

    # qBittorrent
    qbit_host: str
    qbit_api_key: Optional[str]
    qbit_username: Optional[str]
    qbit_password: Optional[str]
    qbit_category: str
    qbit_tags: str
    qbit_save_path: Optional[str]
    qbit_auto_start: bool
    rename_torrent_files: bool

    # Service & Persistence
    check_interval_hours: float
    schedule_days: list[str]
    schedule_times: list[str]
    run_on_startup: bool
    state_file_path: str
    db_path: str

    # Optional Proxy
    proxy_url: Optional[str]

    # Notifications
    discord_webhook_url: Optional[str]
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]

    # Web Server & Log Viewer
    web_port: int
    web_enabled: bool

    @classmethod
    def load(cls) -> "Config":
        raw_times = os.getenv("SCHEDULE_TIMES", "09:00,15:00,21:00").split(",")
        parsed_times = []
        for item in raw_times:
            norm = normalize_schedule_time(item)
            if norm and norm not in parsed_times:
                parsed_times.append(norm)
        parsed_times.sort()

        return cls(
            pier_thread_url=os.getenv(
                "PIER_THREAD_URL", "https://720pier.ru/viewtopic.php?t=76272"
            ).strip(),
            pier_username=os.getenv("PIER_USERNAME", "").strip() or None,
            pier_password=os.getenv("PIER_PASSWORD", "").strip() or None,
            pier_cookie=os.getenv("PIER_COOKIE", "").strip() or None,
            quality_preference=os.getenv("QUALITY_PREFERENCE", "").strip() or None,
            favorite_teams=[
                t.strip()
                for t in os.getenv("FAVORITE_TEAMS", "Kansas City Chiefs, KC, Chiefs").split(",")
                if t.strip()
            ],
            proxy_url=os.getenv("PROXY_URL", "").strip() or None,
            qbit_host=os.getenv("QBIT_HOST", "http://qbittorrent:8080").strip(),
            qbit_api_key=os.getenv("QBIT_API_KEY", "").strip() or None,
            qbit_username=os.getenv("QBIT_USERNAME", "").strip() or None,
            qbit_password=os.getenv("QBIT_PASSWORD", "").strip() or None,
            qbit_category=os.getenv("QBIT_CATEGORY", "NFL").strip(),
            qbit_tags=os.getenv("QBIT_TAGS", "720pier,thuuz").strip(),
            qbit_save_path=os.getenv("QBIT_SAVE_PATH", "/nfl").strip() or None,
            qbit_auto_start=os.getenv("QBIT_AUTO_START", "true").lower() in ("true", "1", "yes"),
            rename_torrent_files=os.getenv("RENAME_TORRENT_FILES", "true").lower() in ("true", "1", "yes"),
            check_interval_hours=float(os.getenv("CHECK_INTERVAL_HOURS", "6")),
            schedule_days=[
                d.strip()
                for d in os.getenv("SCHEDULE_DAYS", "Mon,Tue,Wed").split(",")
                if d.strip()
            ],
            schedule_times=parsed_times,
            run_on_startup=os.getenv("RUN_ON_STARTUP", "false").lower() in ("true", "1", "yes"),
            state_file_path=os.getenv("STATE_FILE_PATH", "data/state.json").strip(),
            db_path=os.getenv("DB_PATH", "data/nfl_grabber.db").strip(),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
            web_port=int(os.getenv("WEB_PORT", "5020")),
            web_enabled=os.getenv("WEB_ENABLED", "true").lower() in ("true", "1", "yes"),
        )
