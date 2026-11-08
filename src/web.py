"""
Lightweight Web Dashboard & Status Server for NFL Torrent Grabber.
Uses standard Python library (no external web framework dependencies).
Provides real-time log streaming, manual triggers, diagnostics, and history management.
"""
import collections
import datetime
import html
import http.server
import json
import logging
import os
import sqlite3
import threading
import urllib.parse
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger("NFLGrabber.Web")

# Thread-safe in-memory ring-buffer for logs
log_buffer: Deque[Dict[str, Any]] = collections.deque(maxlen=3000)
_log_lock = threading.Lock()
_log_counter = 0

# Execution coordinator lock
_cycle_lock = threading.Lock()

# Runtime service status metadata
_service_info: Dict[str, Any] = {
    "status": "Running",
    "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "next_run": "Calculating...",
    "last_run": "None yet",
    "active_week": "Scanning...",
    "db_path": None,
    "is_cycle_running": False,
}

# Injected service context references
_service_context: Dict[str, Any] = {
    "config": None,
    "state": None,
    "notifier": None,
    "cycle_runner": None,
    "schedule_logger": None,
    "schedule_reloader": None,
}

# Cached active week matches
_cached_matches_lock = threading.Lock()
_cached_week_data: Dict[str, Any] = {
    "week": None,
    "fetched_at": None,
    "matches": [],
}


def update_cached_matches(week: str, matches: List[Dict[str, Any]]):
    """Thread-safe update of cached active week matches."""
    with _cached_matches_lock:
        _cached_week_data["week"] = week
        _cached_week_data["fetched_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _cached_week_data["matches"] = matches


def update_service_status(**kwargs):
    """Thread-safe update of service runtime status."""
    with _log_lock:
        _service_info.update(kwargs)
        _service_info["is_cycle_running"] = _cycle_lock.locked()


def get_total_grabbed_torrents(db_path: Optional[str] = None) -> int:
    """Reads historical torrent count from the SQLite database."""
    target_path = db_path or _service_info.get("db_path")
    if not target_path or not os.path.isfile(target_path):
        return 0
    try:
        conn = sqlite3.connect(target_path, timeout=2.0)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM processed_torrents;")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def run_cycle_synchronized(
    dry_run: bool = False,
    caller: str = "scheduler",
    favorite_only: Optional[bool] = None,
) -> tuple[bool, str]:
    """
    Executes a grabber cycle under an exclusive lock.
    Prevents overlapping execution between scheduled and manual runs.
    """
    if not _cycle_lock.acquire(blocking=False):
        logger.warning(
            "Cycle trigger skipped: grabber cycle already running (requested by %s).",
            caller,
        )
        return False, "Grabber cycle already in progress"

    try:
        update_service_status(status="Active Cycle Running", is_cycle_running=True)
        runner = _service_context.get("cycle_runner")
        if runner:
            runner(dry_run=dry_run, favorite_only=favorite_only)
        update_service_status(
            status="Running",
            last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            is_cycle_running=False,
        )
        sched = _service_context.get("schedule_logger")
        if sched:
            try:
                sched()
            except Exception:
                pass
        return True, "Grabber cycle completed successfully"
    except Exception as e:
        logger.error("Error during grabber cycle: %s", e)
        update_service_status(status="Error", is_cycle_running=False)
        return False, str(e)
    finally:
        _cycle_lock.release()
        update_service_status(is_cycle_running=False)


def trigger_cycle_async(dry_run: bool = False, favorite_only: bool = False) -> tuple[bool, str]:
    """Spawns an asynchronous grabber cycle in a daemon thread if lock is free."""
    if _cycle_lock.locked():
        return False, "Grabber cycle already in progress"

    t = threading.Thread(
        target=run_cycle_synchronized,
        kwargs={"dry_run": dry_run, "caller": "web_ui", "favorite_only": favorite_only},
        daemon=True,
        name="ManualGrabberCycle",
    )
    t.start()
    mode = "Dry Run (Preview)" if dry_run else "Standard Run"
    return True, f"{mode} initiated in background."


# --------------------------------------------------------------------------
# Diagnostics & Data Helpers
# --------------------------------------------------------------------------

def run_qbit_diagnostic() -> Dict[str, Any]:
    """Tests qBittorrent connection, credentials, and API version."""
    cfg = _service_context.get("config")
    if not cfg:
        return {"ok": False, "error": "Service configuration not loaded"}
    try:
        from src.qbittorrent import QBittorrentClient

        qbit = QBittorrentClient(
            host=cfg.qbit_host,
            api_key=cfg.qbit_api_key,
            username=cfg.qbit_username,
            password=cfg.qbit_password,
            category=cfg.qbit_category,
            tags=cfg.qbit_tags,
            save_path=cfg.qbit_save_path,
        )
        client = qbit.get_client()
        version = getattr(client.app, "version", "unknown")
        web_api = getattr(client.app, "web_api_version", "unknown")
        return {
            "ok": True,
            "version": version,
            "web_api_version": web_api,
            "host": cfg.qbit_host,
            "category": cfg.qbit_category,
            "save_path": cfg.qbit_save_path,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "host": getattr(cfg, "qbit_host", "")}


def run_pier_diagnostic() -> Dict[str, Any]:
    """Validates 720pier forum connection and session cookie freshness."""
    cfg = _service_context.get("config")
    if not cfg:
        return {"ok": False, "error": "Service configuration not loaded"}
    try:
        from bs4 import BeautifulSoup
        from src.scraper import PierScraper

        scraper = PierScraper(
            thread_url=cfg.pier_thread_url,
            username=cfg.pier_username,
            password=cfg.pier_password,
            cookie_string=cfg.pier_cookie,
            proxy_url=cfg.proxy_url,
        )
        html_doc = scraper.fetch_thread_html()
        soup = BeautifulSoup(html_doc, "lxml")
        week_title, _ = scraper.find_latest_week_section(soup)
        return {
            "ok": True,
            "detected_week": week_title,
            "thread_url": cfg.pier_thread_url,
            "has_cookie": bool(cfg.pier_cookie),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "thread_url": getattr(cfg, "pier_thread_url", "")}


def run_alert_diagnostic() -> Dict[str, Any]:
    """Dispatches a test notification to configured alert channels."""
    notifier = _service_context.get("notifier")
    if not notifier:
        return {"ok": False, "error": "Notifier service not initialized"}
    try:
        results = notifier.send_test_notification()
        return {"ok": True, "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_processed_torrents_data(limit: int = 150) -> List[Dict[str, Any]]:
    """Fetches recently processed torrents from database."""
    state = _service_context.get("state")
    if state and hasattr(state, "get_processed_torrents"):
        return state.get_processed_torrents(limit=limit)

    target_path = _service_info.get("db_path")
    if not target_path or not os.path.isfile(target_path):
        return []
    try:
        conn = sqlite3.connect(target_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT id, torrent_url, title, week, rating, added_at
            FROM processed_torrents
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def delete_processed_torrent_record(identifier: str) -> bool:
    """Removes a processed torrent from SQLite state."""
    state = _service_context.get("state")
    if state and hasattr(state, "delete_processed_torrent"):
        return state.delete_processed_torrent(identifier)

    target_path = _service_info.get("db_path")
    if not target_path or not os.path.isfile(target_path):
        return False
    try:
        conn = sqlite3.connect(target_path, timeout=3.0)
        with conn:
            if str(identifier).isdigit():
                cur = conn.execute("DELETE FROM processed_torrents WHERE id = ?", (int(identifier),))
                if cur.rowcount > 0:
                    conn.close()
                    return True
            cur = conn.execute("DELETE FROM processed_torrents WHERE torrent_url = ?", (str(identifier),))
            res = cur.rowcount > 0
        conn.close()
        return res
    except Exception:
        return False


def get_runs_data(limit: int = 50) -> List[Dict[str, Any]]:
    """Fetches run history records from SQLite database."""
    state = _service_context.get("state")
    if state and hasattr(state, "get_recent_runs"):
        return state.get_recent_runs(limit=limit)

    target_path = _service_info.get("db_path")
    if not target_path or not os.path.isfile(target_path):
        return []
    try:
        conn = sqlite3.connect(target_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT id, started_at, completed_at, status, week_title, torrents_added_count, next_run_at, error_message
            FROM runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def fetch_active_week_matches(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Fetches matches for the active week.
    Returns cached list if available unless force_refresh is True.
    Updates each match's is_grabbed status against the database.
    """
    state = _service_context.get("state")
    with _cached_matches_lock:
        if not force_refresh and _cached_week_data.get("matches"):
            week = _cached_week_data.get("week")
            for m in _cached_week_data["matches"]:
                if state:
                    if hasattr(state, "is_match_or_url_processed"):
                        m["is_grabbed"] = state.is_match_or_url_processed(m.get("url", ""), m.get("title", ""), week=week)
                    else:
                        m["is_grabbed"] = state.is_torrent_processed(m.get("url", ""))
            return {
                "ok": True,
                "week": _cached_week_data.get("week", "Active Week"),
                "fetched_at": _cached_week_data.get("fetched_at"),
                "count": len(_cached_week_data["matches"]),
                "matches": list(_cached_week_data["matches"]),
                "source": "cache",
            }

    cfg = _service_context.get("config")
    if not cfg:
        return {"ok": False, "error": "Configuration not loaded", "matches": []}

    try:
        from bs4 import BeautifulSoup
        from src.scraper import PierScraper

        scraper = PierScraper(
            thread_url=cfg.pier_thread_url,
            username=cfg.pier_username,
            password=cfg.pier_password,
            cookie_string=cfg.pier_cookie,
            quality_preference=cfg.quality_preference,
            proxy_url=cfg.proxy_url,
        )
        html_doc = scraper.fetch_thread_html()
        soup = BeautifulSoup(html_doc, "lxml")
        week_title, week_elem = scraper.find_latest_week_section(soup)
        raw_matches = scraper.get_all_matches_in_week(week_elem)

        processed = []
        for m in raw_matches:
            title = m.get("title", "")
            url = m.get("url", "")
            rating = m.get("rating", "N/A")
            is_g = False
            if state:
                if hasattr(state, "is_match_or_url_processed"):
                    is_g = state.is_match_or_url_processed(url, title, week=week_title)
                else:
                    is_g = state.is_torrent_processed(url)
            is_fav = scraper.matches_favorite_team(title, cfg.favorite_teams)
            processed.append({
                "title": title,
                "rating": rating,
                "url": url,
                "is_grabbed": is_g,
                "is_favorite": is_fav,
            })

        update_cached_matches(week_title, processed)
        update_service_status(active_week=week_title)

        with _cached_matches_lock:
            return {
                "ok": True,
                "week": week_title,
                "fetched_at": _cached_week_data["fetched_at"],
                "count": len(processed),
                "matches": list(processed),
                "source": "live",
            }
    except Exception as e:
        logger.error("Failed to fetch active week matches from 720pier: %s", e)
        return {"ok": False, "error": str(e), "matches": []}


def download_single_match(url: str, title: str, rating: Optional[str] = None, week: Optional[str] = None) -> tuple[bool, str]:
    """
    Downloads and forwards a single game release to qBittorrent, records it in SQLite,
    and dispatches a manual grab alert notification.
    """
    cfg = _service_context.get("config")
    state = _service_context.get("state")
    notifier = _service_context.get("notifier")

    if not cfg:
        return False, "Configuration not initialized"

    try:
        from src.scraper import PierScraper
        from src.qbittorrent import QBittorrentClient
        from src.main import format_match_name

        scraper = PierScraper(
            thread_url=cfg.pier_thread_url,
            username=cfg.pier_username,
            password=cfg.pier_password,
            cookie_string=cfg.pier_cookie,
            quality_preference=cfg.quality_preference,
            proxy_url=cfg.proxy_url,
        )

        qbit = QBittorrentClient(
            host=cfg.qbit_host,
            api_key=cfg.qbit_api_key,
            username=cfg.qbit_username,
            password=cfg.qbit_password,
            category=cfg.qbit_category,
            tags=cfg.qbit_tags,
            save_path=cfg.qbit_save_path,
            auto_start=cfg.qbit_auto_start,
        )

        # 1. Resolve game topic page if link is a forum thread
        torrent_url = url
        topic_url = url
        resolved_title = title

        if "viewtopic.php" in url:
            info = scraper._extract_torrent_from_game_topic(url, title)
            if info and info.get("torrent_url"):
                torrent_url = info["torrent_url"]
                resolved_title = info.get("title", title)
            else:
                return False, f"Could not find torrent download link on release page: {url}"

        # 2. Download torrent binary
        fn = f"{resolved_title}.torrent"
        torrent_bytes, filename = scraper.download_torrent(torrent_url, fn, referer=topic_url)

        # 3. Format name
        torrent_meta = {"title": resolved_title, "matchup": title}
        effective_week = week or _service_info.get("active_week") or "NFL"
        custom_name = format_match_name(effective_week, torrent_meta) if cfg.rename_torrent_files else None

        # 4. Push to qBittorrent
        success = qbit.add_torrent_file(torrent_bytes, filename, custom_name=custom_name)
        if not success:
            return False, f"qBittorrent rejected torrent '{filename}'"

        # 5. Record in database
        if state:
            state.record_processed_torrent(
                torrent_url,
                resolved_title,
                effective_week,
                rating=str(rating) if rating and rating != "N/A" else None,
            )

        # 6. Update cache
        with _cached_matches_lock:
            for m in _cached_week_data.get("matches", []):
                if m.get("url") == url or m.get("title") == title:
                    m["is_grabbed"] = True

        # 7. Send notification alert
        if notifier:
            try:
                is_fav = scraper.matches_favorite_team(resolved_title, cfg.favorite_teams)
                item = {
                    "title": resolved_title,
                    "rating": rating or "N/A",
                    "torrent_url": torrent_url,
                    "is_favorite_team": is_fav,
                }
                notifier.notify_torrents_added(
                    effective_week,
                    [item],
                    next_run_str="Manually grabbed via Web UI",
                )
            except Exception as n_err:
                logger.warning("Error dispatching manual grab notification: %s", n_err)

        return True, f"Successfully pushed '{custom_name or resolved_title}' to qBittorrent!"
    except Exception as e:
        logger.exception("Error during manual match download for '%s': %s", title, e)
        return False, str(e)


class MemoryLogHandler(logging.Handler):
    """Logging handler that pipes log records into the in-memory web ring-buffer."""

    def __init__(self, log_file: Optional[str] = None):
        super().__init__()
        self.log_file = log_file
        if self.log_file:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.log_file)), exist_ok=True)
            except Exception:
                pass

    def emit(self, record: logging.LogRecord):
        global _log_counter
        try:
            formatted_msg = self.format(record)
            entry = {
                "id": None,
                "timestamp": datetime.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "raw": formatted_msg,
            }
            with _log_lock:
                _log_counter += 1
                entry["id"] = _log_counter
                log_buffer.append(entry)

            if self.log_file:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(formatted_msg + "\n")
        except Exception:
            self.handleError(record)


# --------------------------------------------------------------------------
# HTML, CSS & JavaScript Web UI
# --------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NFL Grabber — Dashboard & Control Center</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #1e1f29;
      --card-bg: #282a36;
      --card-border: #44475a;
      --text: #f8f8f2;
      --muted: #6272a4;
      --cyan: #8be9fd;
      --green: #50fa7b;
      --orange: #ffb86c;
      --pink: #ff79c6;
      --purple: #bd93f9;
      --red: #ff5555;
      --yellow: #f1fa8c;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      height: 100vh;
      height: 100dvh;
      margin: 0;
      padding: 0;
      overflow: hidden; /* Lock viewport and prevent whole-page stretching */
    }

    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      display: flex;
      flex-direction: column;
    }

    header {
      flex-shrink: 0;
      background-color: var(--card-bg);
      border-bottom: 1px solid var(--card-border);
      padding: 0.75rem 1.25rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.65rem;
    }

    .brand h1 {
      font-size: 1.15rem;
      font-weight: 700;
      color: var(--cyan);
      letter-spacing: 0.02em;
    }

    .badge-status {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: rgba(80, 250, 123, 0.15);
      border: 1px solid rgba(80, 250, 123, 0.35);
      color: var(--green);
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      padding: 0.2rem 0.55rem;
      border-radius: 9999px;
      transition: all 0.2s ease;
    }

    .badge-status.running {
      background: rgba(241, 250, 140, 0.2);
      border-color: rgba(241, 250, 140, 0.5);
      color: var(--yellow);
    }

    .badge-pulse {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background-color: currentColor;
      box-shadow: 0 0 8px currentColor;
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
      100% { opacity: 1; transform: scale(1); }
    }

    /* Stats Ribbon */
    .stats-ribbon {
      flex-shrink: 0;
      background: rgba(40, 42, 54, 0.75);
      border-bottom: 1px solid var(--card-border);
      display: flex;
      flex-wrap: wrap;
      gap: 1.5rem;
      padding: 0.55rem 1.25rem;
      font-size: 0.8rem;
    }

    .stat-item {
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }

    .stat-label {
      color: var(--muted);
      font-weight: 500;
    }

    .stat-val {
      color: var(--yellow);
      font-family: 'Fira Code', monospace;
      font-weight: 600;
    }

    /* Persistent Action & Tab Toolbar */
    .action-toolbar {
      flex-shrink: 0;
      background-color: #1a1b24;
      border-bottom: 1px solid var(--card-border);
      padding: 0.6rem 1.25rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }

    .tabs-group {
      display: flex;
      gap: 0.35rem;
    }

    .tab-btn {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--muted);
      padding: 0.4rem 0.85rem;
      font-size: 0.8rem;
      font-weight: 600;
      border-radius: 6px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      transition: all 0.15s ease;
    }

    .tab-btn:hover {
      border-color: var(--cyan);
      color: var(--text);
    }

    .tab-btn.active {
      background: var(--purple);
      color: #1e1f29;
      border-color: var(--purple);
      font-weight: 700;
    }

    .tab-counter {
      font-size: 0.7rem;
      padding: 0.1rem 0.35rem;
      border-radius: 9999px;
      background: rgba(0, 0, 0, 0.25);
      color: inherit;
    }

    .actions-right {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }

    /* Run Trigger Split Button */
    .split-btn {
      display: inline-flex;
      position: relative;
    }

    .btn-main-trigger {
      background: linear-gradient(135deg, #50fa7b 0%, #3cdb64 100%);
      color: #14151c;
      border: none;
      font-weight: 700;
      font-size: 0.8rem;
      padding: 0.4rem 0.85rem;
      border-radius: 6px 0 0 6px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      transition: filter 0.15s;
    }

    .btn-main-trigger:hover {
      filter: brightness(1.1);
    }

    .btn-main-trigger:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      filter: grayscale(0.5);
    }

    .btn-dropdown-caret {
      background: #3cdb64;
      color: #14151c;
      border: none;
      border-left: 1px solid rgba(0, 0, 0, 0.2);
      padding: 0.4rem 0.55rem;
      border-radius: 0 6px 6px 0;
      cursor: pointer;
      font-size: 0.75rem;
    }

    .dropdown-menu {
      display: none;
      position: absolute;
      top: calc(100% + 4px);
      right: 0;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6);
      z-index: 100;
      min-width: 175px;
      overflow: hidden;
    }

    .dropdown-menu.show {
      display: block;
    }

    .dropdown-item {
      display: block;
      width: 100%;
      text-align: left;
      padding: 0.5rem 0.85rem;
      background: transparent;
      border: none;
      color: var(--text);
      font-size: 0.78rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s;
    }

    .dropdown-item:hover {
      background: var(--card-border);
      color: var(--cyan);
    }

    .btn-diagnostic {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      font-size: 0.76rem;
      font-weight: 600;
      padding: 0.4rem 0.65rem;
      border-radius: 6px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      transition: all 0.15s ease;
    }

    .btn-diagnostic:hover {
      border-color: var(--cyan);
      color: var(--cyan);
    }

    /* Tab Panes Container */
    .tab-content-area {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }

    .tab-pane {
      display: none;
      flex: 1;
      min-height: 0;
      flex-direction: column;
    }

    .tab-pane.active {
      display: flex;
    }

    /* Sub-bar inside tabs */
    .sub-bar {
      flex-shrink: 0;
      padding: 0.6rem 1.25rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.6rem;
      background-color: var(--bg);
      border-bottom: 1px solid rgba(68, 71, 90, 0.4);
    }

    .search-box {
      display: flex;
      align-items: center;
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 0.35rem 0.65rem;
      width: 250px;
      max-width: 100%;
    }

    .search-box input {
      background: transparent;
      border: none;
      color: var(--text);
      font-family: inherit;
      font-size: 0.8rem;
      outline: none;
      width: 100%;
    }

    .search-box input::placeholder {
      color: var(--muted);
    }

    .filter-pills {
      display: flex;
      gap: 0.3rem;
    }

    .pill-btn {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--muted);
      padding: 0.3rem 0.6rem;
      font-size: 0.72rem;
      font-weight: 600;
      border-radius: 4px;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .pill-btn:hover {
      border-color: var(--cyan);
      color: var(--text);
    }

    .pill-btn.active {
      background: var(--purple);
      color: #1e1f29;
      border-color: var(--purple);
      font-weight: 700;
    }

    .btn-sm {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.35rem 0.65rem;
      border-radius: 5px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      transition: all 0.15s ease;
      text-decoration: none;
    }

    .btn-sm:hover {
      border-color: var(--cyan);
      color: var(--cyan);
    }

    .btn-sm.active {
      background: rgba(80, 250, 123, 0.2);
      border-color: var(--green);
      color: var(--green);
    }

    .table-scroll-container::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }
    .table-scroll-container::-webkit-scrollbar-track {
      background: #14151c;
      border-radius: 4px;
    }
    .table-scroll-container::-webkit-scrollbar-thumb {
      background: var(--card-border);
      border-radius: 5px;
      border: 2px solid #14151c;
    }
    .table-scroll-container::-webkit-scrollbar-thumb:hover {
      background: var(--muted);
    }

    /* Tables (Matches & History) */
    .table-container {
      flex: 1;
      min-height: 0;
      margin: 0.6rem 1.25rem 1rem;
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
    }

    .table-scroll-container {
      flex: 1;
      min-height: 0;
      overflow-y: scroll;
      overflow-x: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--card-border) #14151c;
    }

    table.data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8rem;
      text-align: left;
    }

    table.data-table th {
      background-color: #1a1b24;
      color: var(--muted);
      font-weight: 600;
      padding: 0.65rem 0.85rem;
      border-bottom: 1px solid var(--card-border);
      position: sticky;
      top: 0;
      z-index: 2;
    }

    table.data-table td {
      padding: 0.6rem 0.85rem;
      border-bottom: 1px solid rgba(68, 71, 90, 0.35);
      color: var(--text);
    }

    table.data-table tr:hover td {
      background-color: rgba(68, 71, 90, 0.2);
    }

    .tag-badge {
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 700;
    }
    .tag-high { background: rgba(80, 250, 123, 0.2); color: var(--green); border: 1px solid rgba(80, 250, 123, 0.4); }
    .tag-med { background: rgba(255, 184, 108, 0.2); color: var(--orange); border: 1px solid rgba(255, 184, 108, 0.4); }
    .tag-low { background: rgba(98, 114, 164, 0.2); color: var(--muted); border: 1px solid rgba(98, 114, 164, 0.4); }
    .tag-fav { background: rgba(189, 147, 249, 0.2); color: var(--purple); border: 1px solid rgba(189, 147, 249, 0.4); }
    .tag-status-success { background: rgba(80, 250, 123, 0.2); color: var(--green); }
    .tag-status-notorrents { background: rgba(139, 233, 253, 0.2); color: var(--cyan); }
    .tag-status-running { background: rgba(241, 250, 140, 0.2); color: var(--yellow); }
    .tag-status-error { background: rgba(255, 85, 85, 0.2); color: var(--red); }

    .badge-success { background: rgba(80, 250, 123, 0.2); color: var(--green); border: 1px solid rgba(80, 250, 123, 0.4); font-size: 0.72rem; font-weight: 700; padding: 0.2rem 0.5rem; border-radius: 4px; display: inline-block; }

    .btn-grab {
      background: linear-gradient(135deg, #50fa7b 0%, #3cdb64 100%);
      color: #14151c;
      font-weight: 700;
      border: none;
      padding: 0.3rem 0.65rem;
      border-radius: 4px;
      font-size: 0.75rem;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      transition: all 0.15s ease;
    }
    .btn-grab:hover { filter: brightness(1.15); transform: translateY(-1px); }
    .btn-grab:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }

    .settings-container {
      flex: 1;
      min-height: 0;
      margin: 0.6rem 1.25rem 1rem;
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      overflow-y: auto;
      padding: 1.25rem;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
    }
    .settings-section {
      background: rgba(30, 31, 41, 0.6);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 1rem 1.2rem;
      margin-bottom: 1.2rem;
    }
    .settings-section-title {
      font-size: 0.95rem;
      font-weight: 700;
      color: var(--cyan);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      border-bottom: 1px solid rgba(68, 71, 90, 0.4);
      padding-bottom: 0.5rem;
      margin-bottom: 0.85rem;
    }
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 0.85rem;
    }
    .form-group {
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
    }
    .form-group label {
      font-size: 0.75rem;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .form-group input, .form-group select, .form-group textarea {
      background: var(--bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 0.45rem 0.65rem;
      border-radius: 6px;
      font-family: inherit;
      font-size: 0.8rem;
      outline: none;
      transition: border-color 0.15s;
    }
    .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
      border-color: var(--cyan);
    }
    .form-group small {
      font-size: 0.68rem;
      color: var(--muted);
    }
    .form-checkbox {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      cursor: pointer;
      font-size: 0.8rem;
      font-weight: 500;
    }
    .btn-save {
      background: linear-gradient(135deg, #bd93f9 0%, #a470f5 100%);
      color: #14151c;
      font-weight: 700;
      font-size: 0.85rem;
      padding: 0.55rem 1.3rem;
      border-radius: 6px;
      border: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      transition: all 0.15s;
    }
    .btn-save:hover { filter: brightness(1.15); transform: translateY(-1px); }
    .btn-save:disabled { opacity: 0.6; cursor: not-allowed; }

    .btn-reset {
      background: rgba(255, 85, 85, 0.15);
      border: 1px solid rgba(255, 85, 85, 0.35);
      color: var(--red);
      padding: 0.25rem 0.55rem;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn-reset:hover {
      background: var(--red);
      color: #1e1f29;
    }

    /* Toast Notification */
    #toast-container {
      position: fixed;
      bottom: 24px;
      right: 24px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 9999;
      pointer-events: none;
    }

    .toast {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 0.75rem 1rem;
      border-radius: 8px;
      font-size: 0.8rem;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.7);
      display: flex;
      align-items: center;
      gap: 0.6rem;
      min-width: 280px;
      max-width: 420px;
      pointer-events: auto;
      animation: slideIn 0.25s ease-out;
    }
    .toast.toast-success { border-left: 4px solid var(--green); }
    .toast.toast-error { border-left: 4px solid var(--red); }
    .toast.toast-info { border-left: 4px solid var(--cyan); }
    .toast.toast-warning { border-left: 4px solid var(--orange); }

    @keyframes slideIn {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span style="font-size: 1.4rem;">🏈</span>
      <div>
        <h1>NFL Torrent Grabber</h1>
        <div style="font-size: 0.72rem; color: var(--muted);">Automated THUUZ Scraper & qBittorrent Delivery</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 0.75rem;">
      <div class="badge-status" id="badge-status-wrap">
        <span class="badge-pulse"></span>
        <span id="badge-status-text">RUNNING</span>
      </div>
    </div>
  </header>

  <div class="stats-ribbon">
    <div class="stat-item">
      <span class="stat-label">Next Scheduled Run:</span>
      <span class="stat-val" id="stat-next-run">--</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Last Run:</span>
      <span class="stat-val" id="stat-last-run">--</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Grabbed Torrents:</span>
      <span class="stat-val" id="stat-total-torrents">--</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">Active Week:</span>
      <span class="stat-val" id="stat-active-week">--</span>
    </div>
  </div>

  <!-- Persistent Action Toolbar -->
  <div class="action-toolbar">
    <div class="tabs-group">
      <button class="tab-btn active" data-tab="tab-matches">
        <span>🏈</span> Grabbed Matches
        <span class="tab-counter" id="tab-count-torrents">0</span>
      </button>
      <button class="tab-btn" data-tab="tab-browser">
        <span>⚡</span> Active Week Matches
        <span class="tab-counter" id="tab-count-browser">0</span>
      </button>
      <button class="tab-btn" data-tab="tab-history">
        <span>📜</span> Run History
        <span class="tab-counter" id="tab-count-runs">0</span>
      </button>
      <button class="tab-btn" data-tab="tab-settings">
        <span>⚙️</span> Settings
      </button>
    </div>

    <div class="actions-right">
      <!-- Diagnostics -->
      <button class="btn-diagnostic" id="btn-diag-qbit" title="Test qBittorrent connectivity and auth">
        <span>🔌</span> Test qBit
      </button>
      <button class="btn-diagnostic" id="btn-diag-pier" title="Test 720pier cookie & thread fetch">
        <span>🌐</span> Test 720pier
      </button>
      <button class="btn-diagnostic" id="btn-diag-alert" title="Send a test notification alert">
        <span>🔔</span> Send Alert
      </button>

      <!-- Execution Split Button -->
      <div class="split-btn">
        <button class="btn-main-trigger" id="btn-run-now" title="Trigger an immediate live grabber check cycle">
          <span id="run-btn-icon">▶</span> <span id="run-btn-text">Run Check Now</span>
        </button>
        <button class="btn-dropdown-caret" id="btn-run-caret" title="More trigger options">▼</button>
        <div class="dropdown-menu" id="run-dropdown-menu">
          <button class="dropdown-item" id="btn-opt-live">▶ Run Check Now (Live)</button>
          <button class="dropdown-item" id="btn-opt-dryrun">🧪 Dry Run (Preview Only)</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Tab Content Area -->
  <div class="tab-content-area">

    <!-- TAB 1: Grabbed Matches -->
    <div class="tab-pane active" id="tab-matches">
      <div class="sub-bar">
        <div class="search-box">
          <input type="text" id="matches-search" placeholder="Filter matches by team or week...">
        </div>
        <div style="font-size: 0.78rem; color: var(--muted);" id="matches-summary">Loading grabbed games...</div>
        <button class="btn-sm" id="btn-refresh-matches">Refresh List</button>
      </div>

      <div class="table-container">
        <div class="table-scroll-container">
          <table class="data-table" id="matches-table">
            <thead>
              <tr>
                <th style="width: 50px;">#</th>
                <th style="width: 100px;">Week</th>
                <th style="width: 90px;">Rating</th>
                <th>Matchup / Release Title</th>
                <th style="width: 170px;">Grabbed At</th>
                <th style="width: 120px;">Link</th>
                <th style="width: 120px; text-align: center;">Action</th>
              </tr>
            </thead>
            <tbody id="matches-tbody">
              <tr><td colspan="7" style="text-align: center; color: var(--muted); padding: 2rem;">Loading grabbed matches...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB: Active Week Match Browser -->
    <div class="tab-pane" id="tab-browser">
      <div class="sub-bar">
        <div class="search-box">
          <input type="text" id="browser-search" placeholder="Search active week games...">
        </div>
        <div style="font-size: 0.78rem; color: var(--muted);" id="browser-summary">Loading active week games...</div>
        <button class="btn-sm" id="btn-refresh-browser">🔄 Refresh from 720pier</button>
      </div>

      <div class="table-container">
        <div class="table-scroll-container">
          <table class="data-table" id="browser-table">
            <thead>
              <tr>
                <th style="width: 50px;">#</th>
                <th style="width: 90px;">Rating</th>
                <th>Matchup / Release Title</th>
                <th style="width: 120px; text-align: center;">Favorite</th>
                <th style="width: 120px;">Topic Page</th>
                <th style="width: 170px; text-align: center;">Action</th>
              </tr>
            </thead>
            <tbody id="browser-tbody">
              <tr><td colspan="6" style="text-align: center; color: var(--muted); padding: 2rem;">Loading games for active week...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 2: Run History -->
    <div class="tab-pane" id="tab-history">
      <div class="sub-bar">
        <div style="font-size: 0.78rem; color: var(--muted);" id="history-summary">Recent execution cycles recorded in SQLite</div>
        <button class="btn-sm" id="btn-refresh-history">Refresh History</button>
      </div>

      <div class="table-container">
        <div class="table-scroll-container">
          <table class="data-table" id="history-table">
            <thead>
              <tr>
                <th style="width: 70px;">Run #</th>
                <th style="width: 130px;">Status</th>
                <th style="width: 110px;">Week</th>
                <th style="width: 120px;">Torrents Added</th>
                <th style="width: 170px;">Started At</th>
                <th style="width: 170px;">Completed At</th>
                <th style="width: 100px;">Duration</th>
                <th>Error Details</th>
              </tr>
            </thead>
            <tbody id="history-tbody">
              <tr><td colspan="8" style="text-align: center; color: var(--muted); padding: 2rem;">Loading run history...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB: Settings Management -->
    <div class="tab-pane" id="tab-settings">
      <div class="sub-bar">
        <div style="font-size: 0.8rem; font-weight: 600; color: var(--cyan);">Application Configuration (.env)</div>
        <div style="font-size: 0.75rem; color: var(--muted);">Changes are saved to disk and hot-reloaded immediately</div>
        <button class="btn-sm" id="btn-reload-settings">Reset Form</button>
      </div>

      <div class="settings-container">
        <form id="settings-form" onsubmit="event.preventDefault(); saveSettings();">
          <!-- Section 1 -->
          <div class="settings-section">
            <div class="settings-section-title"><span>🏈</span> Match &amp; Grabber Preferences</div>
            <div class="settings-grid">
              <div class="form-group">
                <label>Favorite Teams (comma-separated)</label>
                <input type="text" id="cfg-FAVORITE_TEAMS" placeholder="Kansas City Chiefs, KC, Chiefs">
                <small>Matches involving these teams are always grabbed even if outside top rated matches</small>
              </div>
              <div class="form-group">
                <label>Top Matches Limit</label>
                <input type="number" id="cfg-TOP_MATCHES_LIMIT" min="1" max="10" placeholder="1">
                <small>Number of highest-rated matches to grab per week (1 = top match only)</small>
              </div>
              <div class="form-group">
                <label>Quality Preference</label>
                <select id="cfg-QUALITY_PREFERENCE">
                  <option value="">Any (first available)</option>
                  <option value="1080p">1080p</option>
                  <option value="720p">720p</option>
                </select>
                <small>Filter torrents by resolution when multiple releases exist</small>
              </div>
              <div class="form-group" style="justify-content: center;">
                <label class="form-checkbox">
                  <input type="checkbox" id="cfg-RENAME_TORRENT_FILES">
                  <span>Standardize File Names (e.g. Week 08 - Team A at Team B)</span>
                </label>
                <label class="form-checkbox" style="margin-top: 0.5rem;">
                  <input type="checkbox" id="cfg-QBIT_AUTO_START">
                  <span>Auto-start downloads in qBittorrent</span>
                </label>
              </div>
            </div>
          </div>

          <!-- Section 2 -->
          <div class="settings-section">
            <div class="settings-section-title"><span>⏰</span> Service Schedule &amp; Timing</div>
            <div class="settings-grid">
              <div class="form-group">
                <label>Schedule Days</label>
                <input type="text" id="cfg-SCHEDULE_DAYS" placeholder="Mon,Tue,Wed,Thu,Fri,Sat,Sun">
                <small>Days of week the grabber runs (comma-separated)</small>
              </div>
              <div class="form-group">
                <label>Schedule Times (HH:MM or 12h)</label>
                <input type="text" id="cfg-SCHEDULE_TIMES" placeholder="09:00,15:00,21:00">
                <small>Specific times of day to trigger grabber checks</small>
              </div>
              <div class="form-group">
                <label>Top Matches Days</label>
                <input type="text" id="cfg-TOP_MATCHES_DAYS" placeholder="Mon,Tue,Wed">
                <small>Days to evaluate top rated games (other days grab favorite teams only)</small>
              </div>
              <div class="form-group">
                <label>Interval Hours (Fallback)</label>
                <input type="number" id="cfg-CHECK_INTERVAL_HOURS" step="0.5" min="0.5" placeholder="6">
                <small>Used only if Schedule Times is empty</small>
              </div>
            </div>
          </div>

          <!-- Section 3 -->
          <div class="settings-section">
            <div class="settings-section-title"><span>🌐</span> 720pier Tracker Connection</div>
            <div class="settings-grid">
              <div class="form-group" style="grid-column: span 2;">
                <label>Thread URL</label>
                <input type="text" id="cfg-PIER_THREAD_URL" placeholder="https://720pier.ru/viewtopic.php?t=76272">
              </div>
              <div class="form-group">
                <label>Username</label>
                <input type="text" id="cfg-PIER_USERNAME" placeholder="720pier forum username">
              </div>
              <div class="form-group">
                <label>Password</label>
                <input type="password" id="cfg-PIER_PASSWORD" placeholder="Leave unchanged to keep existing">
              </div>
              <div class="form-group" style="grid-column: span 2;">
                <label>Session Cookie (PIER_COOKIE)</label>
                <textarea id="cfg-PIER_COOKIE" rows="2" placeholder="Leave unchanged to keep existing or paste updated browser cookie"></textarea>
                <small>Used to authenticate if Cloudflare or CAPTCHA is present on 720pier</small>
              </div>
              <div class="form-group" style="grid-column: span 2;">
                <label>Proxy URL (Optional)</label>
                <input type="text" id="cfg-PROXY_URL" placeholder="socks5://127.0.0.1:1080 or http://proxy:8080">
              </div>
            </div>
          </div>

          <!-- Section 4 -->
          <div class="settings-section">
            <div class="settings-section-title"><span>⚡</span> qBittorrent Configuration</div>
            <div class="settings-grid">
              <div class="form-group">
                <label>Host URL</label>
                <input type="text" id="cfg-QBIT_HOST" placeholder="http://qbittorrent:8087">
              </div>
              <div class="form-group">
                <label>API Key (qBittorrent v5.2+)</label>
                <input type="password" id="cfg-QBIT_API_KEY" placeholder="Leave unchanged to keep existing">
              </div>
              <div class="form-group">
                <label>Username (Fallback)</label>
                <input type="text" id="cfg-QBIT_USERNAME" placeholder="admin">
              </div>
              <div class="form-group">
                <label>Password (Fallback)</label>
                <input type="password" id="cfg-QBIT_PASSWORD" placeholder="Leave unchanged to keep existing">
              </div>
              <div class="form-group">
                <label>Save Path</label>
                <input type="text" id="cfg-QBIT_SAVE_PATH" placeholder="/nfl">
              </div>
              <div class="form-group">
                <label>Category</label>
                <input type="text" id="cfg-QBIT_CATEGORY" placeholder="NFL">
              </div>
              <div class="form-group">
                <label>Tags</label>
                <input type="text" id="cfg-QBIT_TAGS" placeholder="720pier,thuuz">
              </div>
            </div>
          </div>

          <!-- Section 5 -->
          <div class="settings-section">
            <div class="settings-section-title"><span>🔔</span> Notifications (Discord &amp; Telegram)</div>
            <div class="settings-grid">
              <div class="form-group" style="grid-column: span 2;">
                <label>Discord Webhook URL</label>
                <input type="password" id="cfg-DISCORD_WEBHOOK_URL" placeholder="Leave unchanged to keep existing">
              </div>
              <div class="form-group">
                <label>Telegram Bot Token</label>
                <input type="password" id="cfg-TELEGRAM_BOT_TOKEN" placeholder="Leave unchanged to keep existing">
              </div>
              <div class="form-group">
                <label>Telegram Chat ID</label>
                <input type="text" id="cfg-TELEGRAM_CHAT_ID" placeholder="e.g. 123456789">
              </div>
            </div>
          </div>

          <!-- Section 6 -->
          <div class="settings-section">
            <div class="settings-section-title"><span>🔒</span> Dashboard Security (HTTP Basic Auth)</div>
            <div class="settings-grid">
              <div class="form-group">
                <label>Web Dashboard Username</label>
                <input type="text" id="cfg-WEB_USERNAME" placeholder="Leave blank to disable authentication">
                <small>Requires browser login prompt when accessing dashboard</small>
              </div>
              <div class="form-group">
                <label>Web Dashboard Password</label>
                <input type="password" id="cfg-WEB_PASSWORD" placeholder="Leave unchanged to keep existing">
              </div>
            </div>
          </div>

          <!-- Save Button Bar -->
          <div style="display: flex; align-items: center; justify-content: flex-end; gap: 1rem; padding: 0.5rem 0 1.5rem;">
            <span id="settings-status" style="font-size: 0.8rem; color: var(--green);"></span>
            <button type="submit" class="btn-save" id="btn-save-settings">
              <span>💾</span> Save &amp; Apply Settings
            </button>
          </div>
        </form>
      </div>
    </div>

  </div>

  <!-- Toast Alerts Container -->
  <div id="toast-container"></div>

  <script>
    // State variables
    let matchesData = [];
    let previousCycleRunning = false;

    // Elements
    const runBtn = document.getElementById('btn-run-now');
    const runCaret = document.getElementById('btn-run-caret');
    const runDropdown = document.getElementById('run-dropdown-menu');
    const toastContainer = document.getElementById('toast-container');

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function showToast(message, type = 'info', duration = 4000) {
      const toast = document.createElement('div');
      toast.className = `toast toast-${type}`;
      const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : type === 'warning' ? '⚠️' : 'ℹ️';
      toast.innerHTML = `<span>${icon}</span><div style="flex:1;">${escapeHtml(message)}</div>`;
      toastContainer.appendChild(toast);
      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease-out';
        setTimeout(() => toast.remove(), 300);
      }, duration);
    }

    // Tab Switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

        btn.classList.add('active');
        const targetId = btn.dataset.tab;
        document.getElementById(targetId).classList.add('active');

        if (targetId === 'tab-matches') fetchMatches();
        if (targetId === 'tab-browser') fetchBrowserMatches();
        if (targetId === 'tab-history') fetchHistory();
        if (targetId === 'tab-settings') loadSettings();
      });
    });

    // Execution Dropdown & Triggers
    runCaret.addEventListener('click', (e) => {
      e.stopPropagation();
      runDropdown.classList.toggle('show');
    });

    document.addEventListener('click', () => {
      runDropdown.classList.remove('show');
    });

    async function triggerCycle(dryRun = false) {
      runDropdown.classList.remove('show');
      runBtn.disabled = true;
      document.getElementById('run-btn-text').textContent = dryRun ? 'Starting Dry Run...' : 'Starting Cycle...';

      try {
        const res = await fetch(`/api/trigger?dry_run=${dryRun ? '1' : '0'}`, { method: 'POST' });
        const data = await res.json();
        if (res.status === 202) {
          showToast(data.message || 'Cycle successfully started in background!', 'success');
        } else if (res.status === 409) {
          showToast(data.error || 'A grabber cycle is already in progress.', 'warning');
        } else {
          showToast(data.error || 'Failed to trigger cycle.', 'error');
        }
      } catch (err) {
        showToast('Network error triggering grabber cycle: ' + err.message, 'error');
      } finally {
        setTimeout(() => {
          fetchStatus();
          fetchMatches();
          fetchHistory();
        }, 600);
      }
    }

    runBtn.addEventListener('click', () => triggerCycle(false));
    document.getElementById('btn-opt-live').addEventListener('click', () => triggerCycle(false));
    document.getElementById('btn-opt-dryrun').addEventListener('click', () => triggerCycle(true));

    // Diagnostics
    document.getElementById('btn-diag-qbit').addEventListener('click', async () => {
      const btn = document.getElementById('btn-diag-qbit');
      btn.disabled = true;
      btn.textContent = 'Testing...';
      try {
        const res = await fetch('/api/test/qbit', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          showToast(`qBittorrent Connected! Version: ${data.version} (API: ${data.web_api_version})`, 'success', 5000);
        } else {
          showToast(`qBittorrent Test Failed: ${data.error}`, 'error', 6000);
        }
      } catch (e) {
        showToast('qBittorrent connection request failed: ' + e.message, 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>🔌</span> Test qBit';
      }
    });

    document.getElementById('btn-diag-pier').addEventListener('click', async () => {
      const btn = document.getElementById('btn-diag-pier');
      btn.disabled = true;
      btn.textContent = 'Testing...';
      try {
        const res = await fetch('/api/test/pier', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          showToast(`720pier Session Active! Detected: ${data.detected_week}`, 'success', 5000);
        } else {
          showToast(`720pier Session Error: ${data.error}`, 'error', 6000);
        }
      } catch (e) {
        showToast('720pier test request failed: ' + e.message, 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>🌐</span> Test 720pier';
      }
    });

    document.getElementById('btn-diag-alert').addEventListener('click', async () => {
      const btn = document.getElementById('btn-diag-alert');
      btn.disabled = true;
      btn.textContent = 'Sending...';
      try {
        const res = await fetch('/api/test/alert', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          const discord = data.results.discord === true ? 'Discord: Sent' : data.results.discord;
          const telegram = data.results.telegram === true ? 'Telegram: Sent' : data.results.telegram;
          showToast(`Test Alert Dispatched! (${discord}, ${telegram})`, 'success', 5000);
        } else {
          showToast(`Alert Test Failed: ${data.error}`, 'error', 6000);
        }
      } catch (e) {
        showToast('Alert test request failed: ' + e.message, 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>🔔</span> Send Alert';
      }
    });

    // Grabbed Matches Management
    async function fetchMatches() {
      const tbody = document.getElementById('matches-tbody');
      try {
        const res = await fetch('/api/torrents');
        const data = await res.json();
        matchesData = data.torrents || [];
        renderMatches();
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--red); padding: 2rem;">Error loading matches: ${escapeHtml(err.message)}</td></tr>`;
      }
    }

    function renderMatches() {
      const tbody = document.getElementById('matches-tbody');
      const query = (document.getElementById('matches-search').value || '').toLowerCase().trim();
      const filtered = matchesData.filter(m => {
        const text = `${m.title} ${m.week} ${m.rating || ''}`.toLowerCase();
        return !query || text.includes(query);
      });

      document.getElementById('matches-summary').textContent = `Showing ${filtered.length} of ${matchesData.length} match(es)`;
      document.getElementById('tab-count-torrents').textContent = matchesData.length;

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--muted); padding: 2rem;">No grabbed matches match your search criteria.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map((m, idx) => {
        const ratingVal = parseInt(m.rating, 10);
        let tagClass = 'tag-high';
        if (isNaN(ratingVal)) tagClass = 'tag-med';
        else if (ratingVal < 90) tagClass = 'tag-med';

        const favBadge = (m.title || '').includes('Chiefs') || (m.title || '').includes('KC')
          ? '<span class="tag-badge tag-fav" style="margin-left: 4px;">KC</span>' : '';

        const ratingBadge = m.rating
          ? `<span class="tag-badge ${tagClass}">${escapeHtml(m.rating)} pts</span>${favBadge}`
          : '<span class="tag-badge tag-med">N/A</span>';

        return `<tr>
          <td style="color: var(--muted); font-family: 'Fira Code', monospace;">#${m.id}</td>
          <td><strong style="color: var(--cyan);">${escapeHtml(m.week)}</strong></td>
          <td>${ratingBadge}</td>
          <td><div style="font-weight: 500;">${escapeHtml(m.title)}</div></td>
          <td style="color: var(--muted); font-size: 0.75rem;">${escapeHtml(m.added_at ? m.added_at.replace('T', ' ').substring(0, 19) : '--')}</td>
          <td><a href="${escapeHtml(m.torrent_url)}" target="_blank" style="color: var(--cyan); text-decoration: none; font-size: 0.75rem;">Open Link ↗</a></td>
          <td style="text-align: center;">
            <button class="btn-reset" onclick="resetTorrent(${m.id}, '${escapeHtml(m.title).replace(/'/g, "\\'")}')">Forget</button>
          </td>
        </tr>`;
      }).join('');
    }

    async function resetTorrent(id, title) {
      if (!confirm(`Are you sure you want to remove this match from grabber memory?\n\n"${title}"\n\nThis will allow the grabber to re-evaluate and re-download it in future cycles.`)) {
        return;
      }
      try {
        const res = await fetch('/api/torrents/reset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await res.json();
        if (data.ok) {
          showToast(`Match forgotten: "${title}"`, 'success');
          fetchMatches();
          fetchStatus();
        } else {
          showToast('Failed to reset match: ' + (data.error || 'Unknown error'), 'error');
        }
      } catch (err) {
        showToast('Network error resetting match: ' + err.message, 'error');
      }
    }

    document.getElementById('btn-refresh-matches').addEventListener('click', fetchMatches);
    document.getElementById('matches-search').addEventListener('input', renderMatches);

    // Run History Management
    async function fetchHistory() {
      const tbody = document.getElementById('history-tbody');
      try {
        const res = await fetch('/api/runs');
        const data = await res.json();
        const runs = data.runs || [];
        document.getElementById('tab-count-runs').textContent = runs.length;
        renderHistory(runs);
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--red); padding: 2rem;">Error loading run history: ${escapeHtml(err.message)}</td></tr>`;
      }
    }

    function renderHistory(runs) {
      const tbody = document.getElementById('history-tbody');
      if (runs.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--muted); padding: 2rem;">No cycle executions recorded in history yet.</td></tr>`;
        return;
      }

      tbody.innerHTML = runs.map(r => {
        let statusTag = 'tag-status-success';
        if (r.status === 'ERROR') statusTag = 'tag-status-error';
        else if (r.status === 'NO_TORRENTS') statusTag = 'tag-status-notorrents';
        else if (r.status === 'RUNNING') statusTag = 'tag-status-running';

        let durationStr = '--';
        if (r.started_at && r.completed_at) {
          const s = new Date(r.started_at).getTime();
          const e = new Date(r.completed_at).getTime();
          if (!isNaN(s) && !isNaN(e) && e >= s) {
            durationStr = `${Math.round((e - s) / 1000)}s`;
          }
        }

        const errText = r.error_message
          ? `<span style="color: var(--red); font-size: 0.75rem;">${escapeHtml(r.error_message)}</span>`
          : '<span style="color: var(--muted); font-size: 0.75rem;">None</span>';

        return `<tr>
          <td style="color: var(--muted); font-family: 'Fira Code', monospace;">#${r.id}</td>
          <td><span class="tag-badge ${statusTag}">${escapeHtml(r.status)}</span></td>
          <td><strong style="color: var(--cyan);">${escapeHtml(r.week_title || '--')}</strong></td>
          <td><span style="font-weight: 600; color: ${r.torrents_added_count > 0 ? 'var(--green)' : 'var(--text)'};">${r.torrents_added_count || 0}</span></td>
          <td style="color: var(--muted); font-size: 0.75rem;">${escapeHtml(r.started_at ? r.started_at.replace('T', ' ').substring(0, 19) : '--')}</td>
          <td style="color: var(--muted); font-size: 0.75rem;">${escapeHtml(r.completed_at ? r.completed_at.replace('T', ' ').substring(0, 19) : '--')}</td>
          <td style="font-family: 'Fira Code', monospace; font-size: 0.75rem;">${durationStr}</td>
          <td>${errText}</td>
        </tr>`;
      }).join('');
    }

    document.getElementById('btn-refresh-history').addEventListener('click', fetchHistory);

    // Active Week Match Browser Management
    let browserData = [];

    async function fetchBrowserMatches(forceRefresh = false) {
      const tbody = document.getElementById('browser-tbody');
      const summary = document.getElementById('browser-summary');
      const refreshBtn = document.getElementById('btn-refresh-browser');
      if (forceRefresh) {
        refreshBtn.disabled = true;
        refreshBtn.textContent = '🔄 Fetching...';
      }
      summary.textContent = forceRefresh ? 'Scraping 720pier for active week matches...' : 'Loading active week matches...';

      try {
        const url = forceRefresh ? '/api/matches/refresh' : '/api/matches';
        const opts = forceRefresh ? { method: 'POST' } : { method: 'GET' };
        const res = await fetch(url, opts);
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Failed to fetch matches');

        browserData = data.matches || [];
        document.getElementById('tab-count-browser').textContent = browserData.length;
        renderBrowserMatches(data.week || 'Active Week');
        if (forceRefresh) {
          showToast(`Refreshed ${browserData.length} games for ${data.week || 'Active Week'}!`, 'success');
        }
      } catch (err) {
        summary.textContent = 'Error fetching active week games.';
        tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--red); padding: 2rem;">Error: ${escapeHtml(err.message)}</td></tr>`;
        if (forceRefresh) showToast('Failed to refresh matches: ' + err.message, 'error');
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = '🔄 Refresh from 720pier';
      }
    }

    function renderBrowserMatches(weekTitle = 'Active Week') {
      const tbody = document.getElementById('browser-tbody');
      const query = (document.getElementById('browser-search').value || '').toLowerCase().trim();
      const filtered = browserData.filter(m => {
        const text = `${m.title} ${m.rating || ''}`.toLowerCase();
        return !query || text.includes(query);
      });

      document.getElementById('browser-summary').textContent = `${weekTitle} — Showing ${filtered.length} of ${browserData.length} match(es)`;

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--muted); padding: 2rem;">No games match your search criteria.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map((m, idx) => {
        const ratingVal = parseInt(m.rating, 10);
        let tagClass = 'tag-high';
        if (isNaN(ratingVal)) tagClass = 'tag-low';
        else if (ratingVal < 70) tagClass = 'tag-low';
        else if (ratingVal < 90) tagClass = 'tag-med';

        const favBadge = m.is_favorite
          ? '<span class="tag-badge tag-fav">KC Chiefs</span>'
          : '<span style="color: var(--muted); font-size: 0.72rem;">--</span>';

        const ratingBadge = m.rating && m.rating !== 'N/A'
          ? `<span class="tag-badge ${tagClass}">${escapeHtml(m.rating)} pts</span>`
          : '<span class="tag-badge tag-low">N/A</span>';

        let actionCol = '';
        if (m.is_grabbed) {
          actionCol = '<span class="badge-success">✅ Grabbed</span>';
        } else {
          const safeUrl = encodeURIComponent(m.url);
          const safeTitle = encodeURIComponent(m.title);
          const safeRating = encodeURIComponent(m.rating || '');
          const safeWeek = encodeURIComponent(weekTitle);
          actionCol = `<button class="btn-grab" id="grab-btn-${idx}" onclick="manualDownloadMatch('${safeUrl}', '${safeTitle}', '${safeRating}', '${safeWeek}', 'grab-btn-${idx}')">📥 Download to qBit</button>`;
        }

        return `<tr>
          <td style="color: var(--muted); font-family: 'Fira Code', monospace;">#${idx + 1}</td>
          <td>${ratingBadge}</td>
          <td><strong style="font-weight: 600; color: var(--text);">${escapeHtml(m.title)}</strong></td>
          <td style="text-align: center;">${favBadge}</td>
          <td><a href="${escapeHtml(m.url)}" target="_blank" style="color: var(--cyan); text-decoration: none; font-size: 0.75rem;">View Topic ↗</a></td>
          <td style="text-align: center;">${actionCol}</td>
        </tr>`;
      }).join('');
    }

    async function manualDownloadMatch(encUrl, encTitle, encRating, encWeek, btnId) {
      const url = decodeURIComponent(encUrl);
      const title = decodeURIComponent(encTitle);
      const rating = decodeURIComponent(encRating);
      const week = decodeURIComponent(encWeek);
      const btn = document.getElementById(btnId);

      if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ Grabbing...';
      }

      try {
        const res = await fetch('/api/matches/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, title, rating, week })
        });
        const data = await res.json();
        if (data.ok) {
          showToast(data.message || `Successfully sent "${title}" to qBittorrent!`, 'success', 5000);
          if (btn) {
            btn.outerHTML = '<span class="badge-success">✅ Grabbed</span>';
          }
          const item = browserData.find(x => x.url === url || x.title === title);
          if (item) item.is_grabbed = true;
          fetchMatches();
          fetchStatus();
        } else {
          showToast(`Failed to grab match: ${data.error}`, 'error', 6000);
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = '📥 Download to qBit';
          }
        }
      } catch (err) {
        showToast('Network error downloading match: ' + err.message, 'error');
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '📥 Download to qBit';
        }
      }
    }

    document.getElementById('btn-refresh-browser').addEventListener('click', () => fetchBrowserMatches(true));
    document.getElementById('browser-search').addEventListener('input', () => renderBrowserMatches());

    // Settings Management
    async function loadSettings() {
      const status = document.getElementById('settings-status');
      status.textContent = 'Loading settings...';
      status.style.color = 'var(--cyan)';
      try {
        const res = await fetch('/api/settings');
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Failed to load settings');
        const s = data.settings || {};

        document.getElementById('cfg-FAVORITE_TEAMS').value = s.favorite_teams || '';
        document.getElementById('cfg-TOP_MATCHES_LIMIT').value = s.top_matches_limit || 1;
        document.getElementById('cfg-QUALITY_PREFERENCE').value = s.quality_preference || '';
        document.getElementById('cfg-RENAME_TORRENT_FILES').checked = Boolean(s.rename_torrent_files);
        document.getElementById('cfg-QBIT_AUTO_START').checked = Boolean(s.qbit_auto_start);

        document.getElementById('cfg-SCHEDULE_DAYS').value = s.schedule_days || '';
        document.getElementById('cfg-SCHEDULE_TIMES').value = s.schedule_times || '';
        document.getElementById('cfg-TOP_MATCHES_DAYS').value = s.top_matches_days || '';
        document.getElementById('cfg-CHECK_INTERVAL_HOURS').value = s.check_interval_hours || 6;

        document.getElementById('cfg-PIER_THREAD_URL').value = s.pier_thread_url || '';
        document.getElementById('cfg-PIER_USERNAME').value = s.pier_username || '';
        document.getElementById('cfg-PIER_PASSWORD').value = s.has_pier_password ? '********' : '';
        document.getElementById('cfg-PIER_COOKIE').value = s.has_pier_cookie ? '********' : '';
        document.getElementById('cfg-PROXY_URL').value = s.proxy_url || '';

        document.getElementById('cfg-QBIT_HOST').value = s.qbit_host || '';
        document.getElementById('cfg-QBIT_API_KEY').value = s.has_qbit_api_key ? '********' : '';
        document.getElementById('cfg-QBIT_USERNAME').value = s.qbit_username || '';
        document.getElementById('cfg-QBIT_PASSWORD').value = s.has_qbit_password ? '********' : '';
        document.getElementById('cfg-QBIT_SAVE_PATH').value = s.qbit_save_path || '';
        document.getElementById('cfg-QBIT_CATEGORY').value = s.qbit_category || '';
        document.getElementById('cfg-QBIT_TAGS').value = s.qbit_tags || '';

        document.getElementById('cfg-DISCORD_WEBHOOK_URL').value = s.has_discord_webhook ? '********' : '';
        document.getElementById('cfg-TELEGRAM_BOT_TOKEN').value = s.has_telegram_bot ? '********' : '';
        document.getElementById('cfg-TELEGRAM_CHAT_ID').value = s.telegram_chat_id || '';

        document.getElementById('cfg-WEB_USERNAME').value = s.web_username || '';
        document.getElementById('cfg-WEB_PASSWORD').value = s.has_web_auth ? '********' : '';

        status.textContent = 'Configuration loaded.';
        status.style.color = 'var(--green)';
        setTimeout(() => { if (status.textContent === 'Configuration loaded.') status.textContent = ''; }, 3000);
      } catch (err) {
        status.textContent = 'Failed to load configuration: ' + err.message;
        status.style.color = 'var(--red)';
      }
    }

    async function saveSettings() {
      const saveBtn = document.getElementById('btn-save-settings');
      const status = document.getElementById('settings-status');
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span>⏳</span> Saving...';
      status.style.color = 'var(--cyan)';
      status.textContent = 'Writing settings to .env and reloading...';

      const payload = {
        FAVORITE_TEAMS: document.getElementById('cfg-FAVORITE_TEAMS').value,
        TOP_MATCHES_LIMIT: document.getElementById('cfg-TOP_MATCHES_LIMIT').value,
        QUALITY_PREFERENCE: document.getElementById('cfg-QUALITY_PREFERENCE').value,
        RENAME_TORRENT_FILES: document.getElementById('cfg-RENAME_TORRENT_FILES').checked ? 'true' : 'false',
        QBIT_AUTO_START: document.getElementById('cfg-QBIT_AUTO_START').checked ? 'true' : 'false',

        SCHEDULE_DAYS: document.getElementById('cfg-SCHEDULE_DAYS').value,
        SCHEDULE_TIMES: document.getElementById('cfg-SCHEDULE_TIMES').value,
        TOP_MATCHES_DAYS: document.getElementById('cfg-TOP_MATCHES_DAYS').value,
        CHECK_INTERVAL_HOURS: document.getElementById('cfg-CHECK_INTERVAL_HOURS').value,

        PIER_THREAD_URL: document.getElementById('cfg-PIER_THREAD_URL').value,
        PIER_USERNAME: document.getElementById('cfg-PIER_USERNAME').value,
        PIER_PASSWORD: document.getElementById('cfg-PIER_PASSWORD').value,
        PIER_COOKIE: document.getElementById('cfg-PIER_COOKIE').value,
        PROXY_URL: document.getElementById('cfg-PROXY_URL').value,

        QBIT_HOST: document.getElementById('cfg-QBIT_HOST').value,
        QBIT_API_KEY: document.getElementById('cfg-QBIT_API_KEY').value,
        QBIT_USERNAME: document.getElementById('cfg-QBIT_USERNAME').value,
        QBIT_PASSWORD: document.getElementById('cfg-QBIT_PASSWORD').value,
        QBIT_SAVE_PATH: document.getElementById('cfg-QBIT_SAVE_PATH').value,
        QBIT_CATEGORY: document.getElementById('cfg-QBIT_CATEGORY').value,
        QBIT_TAGS: document.getElementById('cfg-QBIT_TAGS').value,

        DISCORD_WEBHOOK_URL: document.getElementById('cfg-DISCORD_WEBHOOK_URL').value,
        TELEGRAM_BOT_TOKEN: document.getElementById('cfg-TELEGRAM_BOT_TOKEN').value,
        TELEGRAM_CHAT_ID: document.getElementById('cfg-TELEGRAM_CHAT_ID').value,

        WEB_USERNAME: document.getElementById('cfg-WEB_USERNAME').value,
        WEB_PASSWORD: document.getElementById('cfg-WEB_PASSWORD').value,
      };

      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.ok) {
          showToast(data.message || 'Settings saved and applied successfully!', 'success', 5000);
          status.style.color = 'var(--green)';
          status.textContent = 'Settings saved and applied successfully!';
          loadSettings();
          fetchStatus();
        } else {
          showToast('Error saving settings: ' + (data.error || 'Unknown error'), 'error', 6000);
          status.style.color = 'var(--red)';
          status.textContent = 'Error: ' + data.error;
        }
      } catch (err) {
        showToast('Network error saving settings: ' + err.message, 'error');
        status.style.color = 'var(--red)';
        status.textContent = 'Network error: ' + err.message;
      } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<span>💾</span> Save &amp; Apply Settings';
      }
    }

    document.getElementById('btn-reload-settings').addEventListener('click', loadSettings);

    // Status Polling
    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        const statusText = (data.status || 'Running').toUpperCase();
        document.getElementById('badge-status-text').textContent = statusText;
        const badgeWrap = document.getElementById('badge-status-wrap');
        const isCycleRunning = data.is_cycle_running || statusText.includes('ACTIVE') || statusText.includes('RUNNING CYCLE');
        badgeWrap.classList.toggle('running', isCycleRunning);

        if (isCycleRunning) {
          runBtn.disabled = true;
          document.getElementById('run-btn-icon').textContent = '⏳';
          document.getElementById('run-btn-text').textContent = 'Cycle Running...';
        } else {
          runBtn.disabled = false;
          document.getElementById('run-btn-icon').textContent = '▶';
          document.getElementById('run-btn-text').textContent = 'Run Check Now';
        }

        if (data.next_run) document.getElementById('stat-next-run').textContent = data.next_run;
        if (data.last_run) document.getElementById('stat-last-run').textContent = data.last_run;
        if (data.active_week) document.getElementById('stat-active-week').textContent = data.active_week;
        if (data.total_torrents !== undefined) {
          document.getElementById('stat-total-torrents').textContent = data.total_torrents;
          document.getElementById('tab-count-torrents').textContent = data.total_torrents;
        }

        if (previousCycleRunning && !isCycleRunning) {
          fetchMatches();
          fetchHistory();
        }
        previousCycleRunning = isCycleRunning;
      } catch (err) {
        // Silently keep current display on transient connection error
      }
    }

    // Initial load and periodic status polling (every 3s)
    fetchStatus();
    fetchMatches();
    fetchBrowserMatches();
    fetchHistory();
    setInterval(fetchStatus, 3000);
  </script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP Request Handler & REST API
# --------------------------------------------------------------------------

class LogRequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for Dashboard UI, REST endpoints, and log exports."""

    def log_request(self, code="-", size="-"):
        pass

    def log_message(self, format, *args):
        # Silence default standard server request logging to avoid recursive log growth
        pass

    def check_auth(self) -> bool:
        """Verifies HTTP Basic Auth if web credentials are configured."""
        cfg = _service_context.get("config")
        if not cfg or not getattr(cfg, "web_username", None) or not getattr(cfg, "web_password", None):
            return True

        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            self.send_auth_challenge()
            return False

        try:
            import base64
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                user, pwd = decoded.split(":", 1)
                if user == cfg.web_username and pwd == cfg.web_password:
                    return True
        except Exception:
            pass

        self.send_auth_challenge()
        return False

    def send_auth_challenge(self):
        """Sends HTTP 401 challenge requesting Basic authentication."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="NFL Grabber Dashboard"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": "Unauthorized"}).encode("utf-8"))

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.check_auth():
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            body = HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/status":
            with _log_lock:
                status_copy = dict(_service_info)
                status_copy["is_cycle_running"] = _cycle_lock.locked()

            total_torrents = get_total_grabbed_torrents(status_copy.get("db_path"))

            payload = {
                "status": status_copy.get("status", "Running"),
                "is_cycle_running": status_copy.get("is_cycle_running", False),
                "started_at": status_copy.get("started_at"),
                "next_run": status_copy.get("next_run"),
                "last_run": status_copy.get("last_run"),
                "active_week": status_copy.get("active_week"),
                "total_torrents": total_torrents,
            }
            self._send_json(200, payload)

        elif path == "/api/matches":
            force_refresh = "refresh" in params and params["refresh"][0].lower() in ("1", "true", "yes")
            res = fetch_active_week_matches(force_refresh=force_refresh)
            self._send_json(200 if res.get("ok", True) else 500, res)

        elif path == "/api/settings":
            cfg = _service_context.get("config")
            if cfg:
                from src.config import get_masked_settings
                self._send_json(200, {"ok": True, "settings": get_masked_settings(cfg)})
            else:
                self._send_json(500, {"ok": False, "error": "Configuration not loaded"})

        elif path == "/api/logs":
            with _log_lock:
                status_copy = dict(_service_info)
                status_copy["is_cycle_running"] = _cycle_lock.locked()

            total_torrents = get_total_grabbed_torrents(status_copy.get("db_path"))

            payload = {
                "status": status_copy.get("status", "Running"),
                "is_cycle_running": status_copy.get("is_cycle_running", False),
                "started_at": status_copy.get("started_at"),
                "next_run": status_copy.get("next_run"),
                "last_run": status_copy.get("last_run"),
                "active_week": status_copy.get("active_week"),
                "total_torrents": total_torrents,
                "total_logs": 0,
                "logs": [],
            }
            self._send_json(200, payload)

        elif path == "/api/torrents":
            limit = 150
            if "limit" in params:
                try:
                    limit = int(params["limit"][0])
                except ValueError:
                    pass
            torrents = get_processed_torrents_data(limit=limit)
            self._send_json(200, {"ok": True, "count": len(torrents), "torrents": torrents})

        elif path == "/api/runs":
            limit = 50
            if "limit" in params:
                try:
                    limit = int(params["limit"][0])
                except ValueError:
                    pass
            runs = get_runs_data(limit=limit)
            self._send_json(200, {"ok": True, "count": len(runs), "runs": runs})

        elif path == "/logs":
            msg = "Logs are available via standard container output ('docker logs nfl-torrent-grabber').\n"
            encoded = msg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_POST(self):
        if not self.check_auth():
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # Parse JSON body if present
        content_length = int(self.headers.get("Content-Length", 0))
        body_data = {}
        if content_length > 0:
            try:
                body_bytes = self.rfile.read(content_length)
                body_data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                body_data = {}

        if path == "/api/trigger":
            dry_run = False
            if "dry_run" in params and params["dry_run"][0].lower() in ("1", "true", "yes"):
                dry_run = True
            elif str(body_data.get("dry_run", "")).lower() in ("1", "true", "yes"):
                dry_run = True

            started, msg = trigger_cycle_async(dry_run=dry_run, favorite_only=False)
            if not started:
                self._send_json(409, {"ok": False, "error": msg})
            else:
                self._send_json(202, {"ok": True, "message": msg, "dry_run": dry_run})

        elif path == "/api/matches/refresh":
            res = fetch_active_week_matches(force_refresh=True)
            self._send_json(200 if res.get("ok", True) else 500, res)

        elif path == "/api/matches/download":
            url = body_data.get("url") or params.get("url", [""])[0]
            title = body_data.get("title") or params.get("title", [""])[0]
            rating = body_data.get("rating") or params.get("rating", [""])[0]
            week = body_data.get("week") or params.get("week", [""])[0]

            if not url or not title:
                self._send_json(400, {"ok": False, "error": "Missing required 'url' or 'title' parameter."})
                return

            success, msg = download_single_match(url, title, rating=rating, week=week)
            self._send_json(200 if success else 500, {"ok": success, "message": msg, "error": msg if not success else None})

        elif path == "/api/settings":
            from src.config import Config, update_env_file, get_masked_settings
            if not body_data:
                self._send_json(400, {"ok": False, "error": "No settings provided in request body."})
                return

            success = update_env_file(body_data)
            if success:
                new_cfg = Config.load()
                _service_context["config"] = new_cfg
                reloader = _service_context.get("schedule_reloader")
                if reloader:
                    try:
                        reloader()
                    except Exception as r_err:
                        logger.warning("Notice updating scheduler: %s", r_err)

                self._send_json(200, {
                    "ok": True,
                    "message": "Settings saved to .env and applied in-memory!",
                    "settings": get_masked_settings(new_cfg),
                })
            else:
                self._send_json(500, {"ok": False, "error": "Failed to update .env configuration file."})

        elif path == "/api/test/qbit":
            res = run_qbit_diagnostic()
            self._send_json(200, res)

        elif path == "/api/test/pier":
            res = run_pier_diagnostic()
            self._send_json(200, res)

        elif path == "/api/test/alert":
            res = run_alert_diagnostic()
            self._send_json(200, res)

        elif path == "/api/torrents/reset":
            ident = body_data.get("id") or body_data.get("torrent_url") or params.get("id", [""])[0] or params.get("torrent_url", [""])[0]
            if not ident:
                self._send_json(400, {"ok": False, "error": "Missing required 'id' or 'torrent_url' field."})
                return

            success = delete_processed_torrent_record(str(ident))
            if success:
                self._send_json(200, {"ok": True, "message": "Torrent record removed from history", "identifier": ident})
            else:
                self._send_json(404, {"ok": False, "error": "Torrent record not found in database"})

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")


def start_web_server(
    port: int = 5020,
    db_path: Optional[str] = None,
    log_file: Optional[str] = None,
    config: Any = None,
    state: Any = None,
    notifier: Any = None,
    cycle_runner: Optional[Callable[..., Any]] = None,
    schedule_logger: Optional[Callable[[], Any]] = None,
    schedule_reloader: Optional[Callable[[], Any]] = None,
):
    """
    Spins up the lightweight Web UI server in a daemon thread.
    Registers service coordinator callbacks for manual triggers, diagnostics, and data operations.
    """
    update_service_status(db_path=db_path)
    _service_context["config"] = config
    _service_context["state"] = state
    _service_context["notifier"] = notifier
    _service_context["cycle_runner"] = cycle_runner
    _service_context["schedule_logger"] = schedule_logger
    _service_context["schedule_reloader"] = schedule_reloader

    class ReusableThreadingServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True

    try:
        server = ReusableThreadingServer(("0.0.0.0", port), LogRequestHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="NFLGrabberWebServer")
        t.start()
        logging.getLogger("NFLGrabber").info("Dashboard & Control Web UI listening at http://0.0.0.0:%d", port)
        return server
    except Exception as e:
        logging.getLogger("NFLGrabber").error("Failed to start web server on port %d: %s", port, e)
        return None
