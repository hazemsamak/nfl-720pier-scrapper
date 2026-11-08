import os
import re
from dataclasses import dataclass
from typing import Any, Optional
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
    top_matches_days: list[str]
    top_matches_limit: int
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
    web_username: Optional[str] = None
    web_password: Optional[str] = None

    @classmethod
    def load(cls, env_path: Optional[str] = None) -> "Config":
        if env_path and os.path.isfile(env_path):
            load_dotenv(env_path, override=True)
        else:
            load_dotenv(override=True)

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
                for d in os.getenv("SCHEDULE_DAYS", "Mon,Tue,Wed,Thu,Fri,Sat,Sun").split(",")
                if d.strip()
            ],
            top_matches_days=[
                d.strip()
                for d in os.getenv("TOP_MATCHES_DAYS", "Mon,Tue,Wed").split(",")
                if d.strip()
            ],
            top_matches_limit=max(1, int(os.getenv("TOP_MATCHES_LIMIT", "1").strip() or 1)) if os.getenv("TOP_MATCHES_LIMIT", "1").strip().isdigit() else 1,
            schedule_times=parsed_times,
            run_on_startup=os.getenv("RUN_ON_STARTUP", "false").lower() in ("true", "1", "yes"),
            state_file_path=os.getenv("STATE_FILE_PATH", "data/state.json").strip(),
            db_path=os.getenv("DB_PATH", "data/nfl_grabber.db").strip(),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
            web_port=int(os.getenv("WEB_PORT", "5020")),
            web_enabled=os.getenv("WEB_ENABLED", "true").lower() in ("true", "1", "yes"),
            web_username=os.getenv("WEB_USERNAME", "").strip() or None,
            web_password=os.getenv("WEB_PASSWORD", "").strip() or None,
        )


def update_env_file(updates: dict[str, Any], env_path: str = ".env") -> bool:
    """
    Safely updates key-value pairs in the .env file while preserving comments and structure.
    Ignores masked secrets (e.g. '********' or starting with '***').
    Also updates os.environ immediately.
    """
    try:
        clean_updates: dict[str, str] = {}
        for k, v in updates.items():
            k_upper = k.strip().upper()
            if not k_upper:
                continue
            v_str = str(v).strip() if v is not None else ""
            # Preserve existing secret if masked value was submitted
            if v_str == "********" or v_str.startswith("****"):
                continue
            clean_updates[k_upper] = v_str

        existing_lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                existing_lines = f.readlines()

        new_lines = []
        handled_keys = set()

        for line in existing_lines:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped or "=" not in stripped:
                new_lines.append(line)
                continue

            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in clean_updates:
                new_lines.append(f"{k}={clean_updates[k]}\n")
                handled_keys.add(k)
                os.environ[k] = clean_updates[k]
            else:
                new_lines.append(line)

        # Append any new keys that were not present in the original .env
        unhandled = [k for k in clean_updates if k not in handled_keys]
        if unhandled:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append("\n# Added via Web UI\n")
            for k in unhandled:
                new_lines.append(f"{k}={clean_updates[k]}\n")
                os.environ[k] = clean_updates[k]

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        return True
    except Exception as e:
        import logging
        logging.getLogger("NFLGrabber.Config").error("Failed to update .env file: %s", e)
        return False


def get_masked_settings(config: Config) -> dict[str, Any]:
    """Returns a serialized dict of current settings with sensitive credentials masked."""
    return {
        "pier_thread_url": config.pier_thread_url,
        "pier_username": config.pier_username or "",
        "pier_password": "********" if config.pier_password else "",
        "has_pier_password": bool(config.pier_password),
        "pier_cookie": "********" if config.pier_cookie else "",
        "has_pier_cookie": bool(config.pier_cookie),
        "quality_preference": config.quality_preference or "",
        "favorite_teams": ", ".join(config.favorite_teams),
        "proxy_url": config.proxy_url or "",
        "qbit_host": config.qbit_host,
        "qbit_api_key": "********" if config.qbit_api_key else "",
        "has_qbit_api_key": bool(config.qbit_api_key),
        "qbit_username": config.qbit_username or "",
        "qbit_password": "********" if config.qbit_password else "",
        "has_qbit_password": bool(config.qbit_password),
        "qbit_category": config.qbit_category,
        "qbit_tags": config.qbit_tags,
        "qbit_save_path": config.qbit_save_path or "",
        "qbit_auto_start": config.qbit_auto_start,
        "rename_torrent_files": config.rename_torrent_files,
        "check_interval_hours": config.check_interval_hours,
        "schedule_days": ", ".join(config.schedule_days),
        "top_matches_days": ", ".join(config.top_matches_days),
        "top_matches_limit": config.top_matches_limit,
        "schedule_times": ", ".join(config.schedule_times),
        "run_on_startup": config.run_on_startup,
        "discord_webhook_url": "********" if config.discord_webhook_url else "",
        "has_discord_webhook": bool(config.discord_webhook_url),
        "telegram_bot_token": "********" if config.telegram_bot_token else "",
        "has_telegram_bot": bool(config.telegram_bot_token),
        "telegram_chat_id": config.telegram_chat_id or "",
        "web_port": config.web_port,
        "web_enabled": config.web_enabled,
        "web_username": config.web_username or "",
        "web_password": "********" if config.web_password else "",
        "has_web_auth": bool(config.web_username and config.web_password),
    }
