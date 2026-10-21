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
}


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


def run_cycle_synchronized(dry_run: bool = False, caller: str = "scheduler") -> tuple[bool, str]:
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
            runner(dry_run=dry_run)
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


def trigger_cycle_async(dry_run: bool = False) -> tuple[bool, str]:
    """Spawns an asynchronous grabber cycle in a daemon thread if lock is free."""
    if _cycle_lock.locked():
        return False, "Grabber cycle already in progress"

    t = threading.Thread(
        target=run_cycle_synchronized,
        kwargs={"dry_run": dry_run, "caller": "web_ui"},
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

    /* Terminal View */
    .terminal-container {
      flex: 1;
      min-height: 0;
      margin: 0.6rem 1.25rem 1rem;
      background-color: #14151c;
      border: 1px solid var(--card-border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    }

    .terminal-header {
      flex-shrink: 0;
      background-color: #1c1d27;
      padding: 0.45rem 0.85rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid rgba(68, 71, 90, 0.6);
      font-size: 0.72rem;
      color: var(--muted);
      font-family: 'Fira Code', monospace;
    }

    .window-dots {
      display: flex;
      gap: 5px;
    }

    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
    }
    .dot-red { background: #ff5555; }
    .dot-yellow { background: #f1fa8c; }
    .dot-green { background: #50fa7b; }

    #log-stream {
      flex: 1;
      min-height: 0;
      padding: 0.85rem 1rem;
      overflow-y: scroll;
      overflow-x: hidden;
      font-family: 'Fira Code', monospace;
      font-size: 0.81rem;
      line-height: 1.5;
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-all;
      scrollbar-width: thin;
      scrollbar-color: var(--card-border) #14151c;
    }

    #log-stream::-webkit-scrollbar,
    .table-scroll-container::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }
    #log-stream::-webkit-scrollbar-track,
    .table-scroll-container::-webkit-scrollbar-track {
      background: #14151c;
      border-radius: 4px;
    }
    #log-stream::-webkit-scrollbar-thumb,
    .table-scroll-container::-webkit-scrollbar-thumb {
      background: var(--card-border);
      border-radius: 5px;
      border: 2px solid #14151c;
    }
    #log-stream::-webkit-scrollbar-thumb:hover,
    .table-scroll-container::-webkit-scrollbar-thumb:hover {
      background: var(--muted);
    }

    .log-line {
      display: block;
      padding: 0.12rem 0.25rem;
      border-radius: 3px;
      transition: background 0.1s;
    }
    .log-line:hover {
      background: rgba(68, 71, 90, 0.3);
    }

    .log-time { color: var(--muted); margin-right: 0.5rem; }
    .log-lvl { font-weight: 700; margin-right: 0.5rem; }
    .log-lvl-INFO { color: var(--cyan); }
    .log-lvl-WARNING { color: var(--orange); }
    .log-lvl-ERROR { color: var(--red); }
    .log-logger { color: var(--purple); margin-right: 0.4rem; }
    .log-msg { color: var(--text); }

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
    .tag-fav { background: rgba(189, 147, 249, 0.2); color: var(--purple); border: 1px solid rgba(189, 147, 249, 0.4); }
    .tag-status-success { background: rgba(80, 250, 123, 0.2); color: var(--green); }
    .tag-status-notorrents { background: rgba(139, 233, 253, 0.2); color: var(--cyan); }
    .tag-status-running { background: rgba(241, 250, 140, 0.2); color: var(--yellow); }
    .tag-status-error { background: rgba(255, 85, 85, 0.2); color: var(--red); }

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
      <button class="tab-btn active" data-tab="tab-logs">
        <span>📋</span> Live Logs
      </button>
      <button class="tab-btn" data-tab="tab-matches">
        <span>🏈</span> Grabbed Matches
        <span class="tab-counter" id="tab-count-torrents">0</span>
      </button>
      <button class="tab-btn" data-tab="tab-history">
        <span>📜</span> Run History
        <span class="tab-counter" id="tab-count-runs">0</span>
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

    <!-- TAB 1: Live Logs -->
    <div class="tab-pane active" id="tab-logs">
      <div class="sub-bar">
        <div class="search-box">
          <input type="text" id="search-input" placeholder="Search / filter logs...">
        </div>

        <div class="filter-pills">
          <button class="pill-btn active" data-lvl="ALL">ALL</button>
          <button class="pill-btn" data-lvl="INFO">INFO</button>
          <button class="pill-btn" data-lvl="WARNING">WARN</button>
          <button class="pill-btn" data-lvl="ERROR">ERROR</button>
        </div>

        <div style="display: flex; align-items: center; gap: 0.4rem; margin-left: auto;">
          <button class="btn-sm active" id="btn-autoscroll">Auto-scroll: ON</button>
          <button class="btn-sm" id="btn-clear-logs">Clear View</button>
          <a class="btn-sm" href="/logs?download=1" download="nfl_grabber.log">Download</a>
          <button class="btn-sm" id="btn-refresh-logs">Refresh</button>
        </div>
      </div>

      <div class="terminal-container">
        <div class="terminal-header">
          <div class="window-dots">
            <div class="dot dot-red"></div>
            <div class="dot dot-yellow"></div>
            <div class="dot dot-green"></div>
          </div>
          <div>docker://nfl-torrent-grabber/stdout &bull; <span style="color: var(--cyan); font-weight: 500;">newest on top</span></div>
          <div id="connection-status" style="color: var(--green);">Connected</div>
        </div>
        <div id="log-stream"></div>
      </div>
    </div>

    <!-- TAB 2: Grabbed Matches -->
    <div class="tab-pane" id="tab-matches">
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

    <!-- TAB 3: Run History -->
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

  </div>

  <!-- Toast Alerts Container -->
  <div id="toast-container"></div>

  <script>
    // State variables
    let allLogs = [];
    let lastSeenId = 0;
    let autoScroll = true;
    let currentFilter = 'ALL';
    let searchQuery = '';
    let matchesData = [];

    // Elements
    const streamEl = document.getElementById('log-stream');
    const searchInput = document.getElementById('search-input');
    const autoScrollBtn = document.getElementById('btn-autoscroll');
    const clearBtn = document.getElementById('btn-clear-logs');
    const refreshBtn = document.getElementById('btn-refresh-logs');
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

    // -------------------------------------------------------------------------
    // Tab Switching
    // -------------------------------------------------------------------------
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

        btn.classList.add('active');
        const targetId = btn.dataset.tab;
        document.getElementById(targetId).classList.add('active');

        if (targetId === 'tab-matches') fetchMatches();
        if (targetId === 'tab-history') fetchHistory();
      });
    });

    // -------------------------------------------------------------------------
    // Execution Dropdown & Triggers
    // -------------------------------------------------------------------------
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
          // Switch to logs tab to view progress immediately
          document.querySelector('[data-tab="tab-logs"]').click();
        } else if (res.status === 409) {
          showToast(data.error || 'A grabber cycle is already in progress.', 'warning');
        } else {
          showToast(data.error || 'Failed to trigger cycle.', 'error');
        }
      } catch (err) {
        showToast('Network error triggering grabber cycle: ' + err.message, 'error');
      } finally {
        setTimeout(fetchLogs, 500);
      }
    }

    runBtn.addEventListener('click', () => triggerCycle(false));
    document.getElementById('btn-opt-live').addEventListener('click', () => triggerCycle(false));
    document.getElementById('btn-opt-dryrun').addEventListener('click', () => triggerCycle(true));

    // -------------------------------------------------------------------------
    // Diagnostic Buttons
    // -------------------------------------------------------------------------
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

    // -------------------------------------------------------------------------
    // Log Stream Functions
    // -------------------------------------------------------------------------
    function renderLogEntry(entry) {
      const line = document.createElement('div');
      line.className = 'log-line';
      line.dataset.level = entry.level;
      line.dataset.id = entry.id;

      const lvlClass = 'log-lvl-' + (entry.level || 'INFO');
      line.innerHTML = `<span class="log-time">${escapeHtml(entry.timestamp)}</span>` +
                       `<span class="log-lvl ${lvlClass}">[${escapeHtml(entry.level)}]</span>` +
                       `<span class="log-logger">${escapeHtml(entry.logger)}:</span>` +
                       `<span class="log-msg">${escapeHtml(entry.message)}</span>`;

      applyVisibility(line);
      return line;
    }

    function applyVisibility(lineEl) {
      const lvl = lineEl.dataset.level || '';
      const text = lineEl.textContent.toLowerCase();

      let visible = true;
      if (currentFilter !== 'ALL' && lvl !== currentFilter) {
        visible = false;
      }
      if (searchQuery && !text.includes(searchQuery)) {
        visible = false;
      }

      lineEl.style.display = visible ? 'block' : 'none';
    }

    function refilterAll() {
      const lines = streamEl.querySelectorAll('.log-line');
      lines.forEach(applyVisibility);
      if (autoScroll) {
        streamEl.scrollTop = 0;
      }
    }

    async function fetchLogs() {
      try {
        const res = await fetch(`/api/logs?since=${lastSeenId}`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        // Update stats
        const statusText = (data.status || 'Running').toUpperCase();
        document.getElementById('badge-status-text').textContent = statusText;
        const badgeWrap = document.getElementById('badge-status-wrap');
        const isCycleRunning = data.is_cycle_running || statusText.includes('ACTIVE') || statusText.includes('RUNNING CYCLE');
        badgeWrap.classList.toggle('running', isCycleRunning);

        // Update trigger button states
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

        if (data.logs && data.logs.length > 0) {
          const newEntries = data.logs.filter(entry => entry.id > lastSeenId);
          if (newEntries.length > 0) {
            newEntries.sort((a, b) => b.id - a.id);
            const highestId = Math.max(...newEntries.map(e => e.id));
            lastSeenId = highestId;

            const frag = document.createDocumentFragment();
            newEntries.forEach(entry => {
              allLogs.unshift(entry);
              frag.appendChild(renderLogEntry(entry));
            });

            if (streamEl.firstChild) {
              streamEl.insertBefore(frag, streamEl.firstChild);
            } else {
              streamEl.appendChild(frag);
            }

            if (autoScroll) {
              streamEl.scrollTop = 0;
            }
          }
        }
        document.getElementById('connection-status').textContent = 'Live';
        document.getElementById('connection-status').style.color = 'var(--green)';
      } catch (err) {
        document.getElementById('connection-status').textContent = 'Reconnecting...';
        document.getElementById('connection-status').style.color = 'var(--orange)';
      }
    }

    autoScrollBtn.addEventListener('click', () => {
      autoScroll = !autoScroll;
      autoScrollBtn.textContent = 'Auto-scroll: ' + (autoScroll ? 'ON' : 'OFF');
      autoScrollBtn.classList.toggle('active', autoScroll);
      if (autoScroll) {
        streamEl.scrollTop = 0;
      }
    });

    streamEl.addEventListener('scroll', () => {
      if (streamEl.scrollTop > 40) {
        if (autoScroll) {
          autoScroll = false;
          autoScrollBtn.textContent = 'Auto-scroll: OFF';
          autoScrollBtn.classList.remove('active');
        }
      } else if (streamEl.scrollTop <= 5) {
        if (!autoScroll) {
          autoScroll = true;
          autoScrollBtn.textContent = 'Auto-scroll: ON';
          autoScrollBtn.classList.add('active');
        }
      }
    });

    clearBtn.addEventListener('click', () => {
      streamEl.innerHTML = '';
    });

    refreshBtn.addEventListener('click', () => {
      streamEl.innerHTML = '';
      lastSeenId = 0;
      allLogs = [];
      fetchLogs();
    });

    searchInput.addEventListener('input', (e) => {
      searchQuery = e.target.value.toLowerCase().trim();
      refilterAll();
    });

    document.querySelectorAll('.pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFilter = btn.dataset.lvl;
        refilterAll();
      });
    });

    // -------------------------------------------------------------------------
    // Grabbed Matches Management (TAB 2)
    // -------------------------------------------------------------------------
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
      if (!confirm(`Are you sure you want to remove this match from grabber memory?\\n\\n"${title}"\\n\\nThis will allow the grabber to re-evaluate and re-download it in future cycles.`)) {
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
          fetchLogs();
        } else {
          showToast('Failed to reset match: ' + (data.error || 'Unknown error'), 'error');
        }
      } catch (err) {
        showToast('Network error resetting match: ' + err.message, 'error');
      }
    }

    document.getElementById('btn-refresh-matches').addEventListener('click', fetchMatches);
    document.getElementById('matches-search').addEventListener('input', renderMatches);

    // -------------------------------------------------------------------------
    // Run History Management (TAB 3)
    // -------------------------------------------------------------------------
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

    // Initial load and periodic background polling (every 2.5s)
    fetchLogs();
    setInterval(fetchLogs, 2500);
  </script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP Request Handler & REST API
# --------------------------------------------------------------------------

class LogRequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for Dashboard UI, REST endpoints, and log exports."""

    def log_message(self, format, *args):
        # Silence default standard server request logging to avoid recursive log growth
        pass

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
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

        elif path == "/api/logs":
            since_id = 0
            if "since" in params:
                try:
                    since_id = int(params["since"][0])
                except ValueError:
                    since_id = 0

            with _log_lock:
                if since_id > 0:
                    entries = [e for e in log_buffer if e["id"] > since_id]
                else:
                    entries = list(log_buffer)

                total_lines = len(log_buffer)
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
                "total_logs": total_lines,
                "logs": entries,
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
            is_download = "download" in params
            reverse_order = "reverse" in params or "desc" in params
            with _log_lock:
                raw_entries = list(reversed(log_buffer)) if reverse_order else list(log_buffer)
                text_content = "\n".join(e["raw"] for e in raw_entries) + "\n"

            encoded = text_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if is_download:
                self.send_header("Content-Disposition", 'attachment; filename="nfl_grabber.log"')
            self.end_headers()
            self.wfile.write(encoded)

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_POST(self):
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

            started, msg = trigger_cycle_async(dry_run=dry_run)
            if not started:
                self._send_json(409, {"ok": False, "error": msg})
            else:
                self._send_json(202, {"ok": True, "message": msg, "dry_run": dry_run})

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
    cycle_runner: Optional[Callable[[bool], Any]] = None,
    schedule_logger: Optional[Callable[[], Any]] = None,
):
    """
    Initializes the in-memory log buffer and spins up the Web UI server in a daemon thread.
    Registers service coordinator callbacks for manual triggers, diagnostics, and data operations.
    """
    update_service_status(db_path=db_path)
    _service_context["config"] = config
    _service_context["state"] = state
    _service_context["notifier"] = notifier
    _service_context["cycle_runner"] = cycle_runner
    _service_context["schedule_logger"] = schedule_logger

    # Attach MemoryLogHandler to the root logger so all logs are captured in memory
    root_logger = logging.getLogger()
    has_mem_handler = any(isinstance(h, MemoryLogHandler) for h in root_logger.handlers)
    if not has_mem_handler:
        mem_handler = MemoryLogHandler(log_file=log_file)
        mem_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        root_logger.addHandler(mem_handler)

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
