"""
Lightweight Web Log Viewer & Status Server for NFL Torrent Grabber.
Uses standard Python library (no external dependencies).
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
from typing import Any, Deque, Dict, List, Optional

# Thread-safe in-memory ring-buffer for logs
log_buffer: Deque[Dict[str, Any]] = collections.deque(maxlen=3000)
_log_lock = threading.Lock()
_log_counter = 0

# Runtime service metadata
_service_info = {
    "status": "Running",
    "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "next_run": "Calculating...",
    "last_run": "None yet",
    "active_week": "Scanning...",
    "db_path": None,
}


def update_service_status(**kwargs):
    """Thread-safe update of service runtime status."""
    with _log_lock:
        _service_info.update(kwargs)


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


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NFL Grabber — Container Logs</title>
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
      padding: 0.85rem 1.25rem;
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
    }

    .badge-pulse {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background-color: var(--green);
      box-shadow: 0 0 8px var(--green);
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
      padding: 0.65rem 1.25rem;
      font-size: 0.82rem;
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

    /* Controls Bar */
    .controls-bar {
      flex-shrink: 0;
      padding: 0.75rem 1.25rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      background-color: var(--bg);
      border-bottom: 1px solid rgba(68, 71, 90, 0.4);
    }

    .search-box {
      display: flex;
      align-items: center;
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;
      padding: 0.4rem 0.75rem;
      width: 260px;
      max-width: 100%;
    }

    .search-box input {
      background: transparent;
      border: none;
      color: var(--text);
      font-family: inherit;
      font-size: 0.85rem;
      outline: none;
      width: 100%;
    }

    .search-box input::placeholder {
      color: var(--muted);
    }

    .filter-pills {
      display: flex;
      gap: 0.35rem;
    }

    .pill-btn {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--muted);
      padding: 0.35rem 0.65rem;
      font-size: 0.75rem;
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

    .action-group {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-left: auto;
    }

    .btn {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      font-size: 0.78rem;
      font-weight: 600;
      padding: 0.4rem 0.75rem;
      border-radius: 6px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      transition: all 0.15s ease;
      text-decoration: none;
    }

    .btn:hover {
      background-color: var(--card-border);
      border-color: var(--cyan);
      color: var(--cyan);
    }

    .btn-toggle.active {
      background: rgba(80, 250, 123, 0.2);
      border-color: var(--green);
      color: var(--green);
    }

    /* Terminal Container */
    .terminal-container {
      flex: 1;
      min-height: 0; /* Crucial: allows flex container to shrink within viewport and enable inside scroll */
      margin: 0.75rem 1.25rem 1.25rem;
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
      padding: 0.5rem 0.85rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid rgba(68, 71, 90, 0.6);
      font-size: 0.75rem;
      color: var(--muted);
      font-family: 'Fira Code', monospace;
    }

    .window-dots {
      display: flex;
      gap: 6px;
    }

    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
    }
    .dot-red { background: #ff5555; }
    .dot-yellow { background: #f1fa8c; }
    .dot-green { background: #50fa7b; }

    #log-stream {
      flex: 1;
      min-height: 0; /* Crucial for scroll container inside flexbox */
      padding: 0.85rem 1rem;
      overflow-y: scroll; /* Forces inside scroll bar to be available */
      overflow-x: hidden;
      font-family: 'Fira Code', monospace;
      font-size: 0.82rem;
      line-height: 1.5;
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-all;
      scrollbar-width: thin;
      scrollbar-color: var(--card-border) #14151c;
    }

    #log-stream::-webkit-scrollbar {
      width: 10px;
    }
    #log-stream::-webkit-scrollbar-track {
      background: #14151c;
      border-radius: 4px;
    }
    #log-stream::-webkit-scrollbar-thumb {
      background: var(--card-border);
      border-radius: 5px;
      border: 2px solid #14151c;
    }
    #log-stream::-webkit-scrollbar-thumb:hover {
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
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span style="font-size: 1.4rem;">🏈</span>
      <div>
        <h1>NFL Torrent Grabber</h1>
        <div style="font-size: 0.75rem; color: var(--muted);">Live Container Log Stream</div>
      </div>
    </div>
    <div style="display: flex; align-items: center; gap: 0.75rem;">
      <div class="badge-status">
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
      <span class="stat-label">Log Buffer:</span>
      <span class="stat-val" id="stat-log-count">0 lines</span>
    </div>
  </div>

  <div class="controls-bar">
    <div class="search-box">
      <input type="text" id="search-input" placeholder="Search / filter logs...">
    </div>

    <div class="filter-pills">
      <button class="pill-btn active" data-lvl="ALL">ALL</button>
      <button class="pill-btn" data-lvl="INFO">INFO</button>
      <button class="pill-btn" data-lvl="WARNING">WARN</button>
      <button class="pill-btn" data-lvl="ERROR">ERROR</button>
    </div>

    <div class="action-group">
      <button class="btn btn-toggle active" id="btn-autoscroll">Auto-scroll: ON</button>
      <button class="btn" id="btn-clear">Clear View</button>
      <a class="btn" href="/logs?download=1" download="nfl_grabber.log">Download</a>
      <button class="btn" id="btn-refresh">Refresh</button>
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

  <script>
    let allLogs = [];
    let lastSeenId = 0;
    let autoScroll = true;
    let currentFilter = 'ALL';
    let searchQuery = '';

    const streamEl = document.getElementById('log-stream');
    const searchInput = document.getElementById('search-input');
    const autoScrollBtn = document.getElementById('btn-autoscroll');
    const clearBtn = document.getElementById('btn-clear');
    const refreshBtn = document.getElementById('btn-refresh');

    function escapeHtml(str) {
      if (!str) return '';
      return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

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
        if (data.status) document.getElementById('badge-status-text').textContent = data.status.toUpperCase();
        if (data.next_run) document.getElementById('stat-next-run').textContent = data.next_run;
        if (data.last_run) document.getElementById('stat-last-run').textContent = data.last_run;
        if (data.total_torrents !== undefined) document.getElementById('stat-total-torrents').textContent = data.total_torrents;
        if (data.total_logs !== undefined) document.getElementById('stat-log-count').textContent = data.total_logs + ' lines';

        if (data.logs && data.logs.length > 0) {
          // Filter to new entries
          const newEntries = data.logs.filter(entry => entry.id > lastSeenId);
          if (newEntries.length > 0) {
            // Sort new entries descending (newest ID first) so newest messages are at the top
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

    // Auto-scroll toggle (maintains scroll at top for latest messages)
    autoScrollBtn.addEventListener('click', () => {
      autoScroll = !autoScroll;
      autoScrollBtn.textContent = 'Auto-scroll: ' + (autoScroll ? 'ON' : 'OFF');
      autoScrollBtn.classList.toggle('active', autoScroll);
      if (autoScroll) {
        streamEl.scrollTop = 0;
      }
    });

    // Detect user manual scroll in the inside scroll container
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

    // Clear view
    clearBtn.addEventListener('click', () => {
      streamEl.innerHTML = '';
    });

    // Manual refresh (clears and reloads entire history newest-first)
    refreshBtn.addEventListener('click', () => {
      streamEl.innerHTML = '';
      lastSeenId = 0;
      allLogs = [];
      fetchLogs();
    });

    // Search input
    searchInput.addEventListener('input', (e) => {
      searchQuery = e.target.value.toLowerCase().trim();
      refilterAll();
    });

    // Filter pills
    document.querySelectorAll('.pill-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFilter = btn.dataset.lvl;
        refilterAll();
      });
    });

    // Initial fetch and 2-second interval polling
    fetchLogs();
    setInterval(fetchLogs, 2000);
  </script>
</body>
</html>
"""


class LogRequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for web UI, raw logs, and REST API."""

    def log_message(self, format, *args):
        # Silence default standard server access logging to prevent cluttering the log
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

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

            total_torrents = get_total_grabbed_torrents(status_copy.get("db_path"))

            payload = {
                "status": status_copy.get("status", "Running"),
                "started_at": status_copy.get("started_at"),
                "next_run": status_copy.get("next_run"),
                "last_run": status_copy.get("last_run"),
                "active_week": status_copy.get("active_week"),
                "total_torrents": total_torrents,
                "total_logs": total_lines,
                "logs": entries,
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        elif path == "/logs":
            is_download = "download" in params
            reverse_order = "reverse" in params or "desc" in params
            with _log_lock:
                raw_entries = list(reversed(log_buffer)) if reverse_order else log_buffer
                text_content = "\n".join(e["raw"] for e in raw_entries) + "\n"

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            if is_download:
                self.send_header("Content-Disposition", 'attachment; filename="nfl_grabber.log"')
            self.end_headers()
            self.wfile.write(text_content.encode("utf-8"))

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")


def start_web_server(port: int = 5020, db_path: Optional[str] = None, log_file: Optional[str] = None):
    """
    Initializes the in-memory log handler and spins up the HTTP server in a daemon thread.
    """
    update_service_status(db_path=db_path)

    # Attach MemoryLogHandler to the root logger so all modules are captured
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
        logging.getLogger("NFLGrabber").info("Embedded Web Log Viewer listening at http://0.0.0.0:%d", port)
        return server
    except Exception as e:
        logging.getLogger("NFLGrabber").error("Failed to start embedded web server on port %d: %s", port, e)
        return None
