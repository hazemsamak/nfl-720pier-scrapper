# NFL 720pier THUUZ Torrent Grabber & qBittorrent Forwarder

An automated, containerized Python service that monitors the NFL THUUZ Game Rating thread on **720pier.ru**, extracts the top 2 highest-rated games of the latest week, downloads the `.torrent` files using authenticated sessions, and immediately pushes them to an existing **qBittorrent** instance.

---

## Features

- **Automated Latest Week Detection**: Scans the THUUZ forum topic, automatically identifies the highest week number (e.g., Week 5, Week 6), and parses games ordered from 100 to 0.
- **Top 2 Torrents Selection**: Automatically selects and downloads the first 2 torrent files listed in the latest week.
- **Topic & Direct Link Resolution**: Seamlessly handles direct `.torrent` attachment links as well as following game topic pages (`viewtopic.php?t=...`) to locate the release torrent file.
- **Flexible 720pier Authentication**: Supports standard username/password login as well as browser session cookie strings (`PIER_COOKIE`) to easily bypass Cloudflare or CAPTCHA challenges.
- **qBittorrent Web API Integration**: Directly uploads `.torrent` binary files, starts downloads immediately, and tags them with category `NFL` and tags `720pier,thuuz`.
- **Deduplication & State Persistence**: Preserves processed torrents in `data/state.json` via Docker volume so games are never re-added across restarts.
- **Optional Webhook Notifications**: Dispatches alerts to Discord and/or Telegram when new matches are added to qBittorrent.
- **Daemon & One-Shot Modes**: Runs continuously in Docker on a scheduled interval (e.g. every 6 hours), or on-demand using `--once`.

---

## Project Structure

```text
.
├── main.py               # Main CLI & service entrypoint launcher
├── src/                  # Core application package
│   ├── __init__.py       # Package exports
│   ├── config.py         # Configuration loader & dataclass (.env)
│   ├── main.py           # Execution loop, scheduling, & cycle logic
│   ├── notifier.py       # Discord & Telegram notifications
│   ├── qbittorrent.py    # qBittorrent Web API client (API key / Bearer auth)
│   ├── scraper.py        # 720pier forum scraper & torrent downloader
│   └── state.py          # State persistence manager (data/state.json)
├── scripts/              # Standalone utility & testing scripts
│   └── test_scrape.py    # Interactive forum scraping tester
├── tests/                # Automated unit test suite
│   ├── __init__.py
│   └── test_grabber.py
├── data/                 # Volume mount directory for persistent state
│   └── state.json
├── Dockerfile            # Container build specification
├── docker-compose.yml    # Docker Compose deployment setup
├── requirements.txt      # Python dependencies
├── .env.example          # Template environment configuration
└── .env                  # Local environment configuration
```

---

## Quick Start (Docker Compose)

### 1. Clone and Configure Environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```ini
# 720pier Credentials
PIER_USERNAME=your_forum_username
PIER_PASSWORD=your_forum_password

# OPTIONAL: If 720pier displays a CAPTCHA or Cloudflare challenge,
# copy your browser's Cookie header string (e.g. phpbb3_... session cookie):
PIER_COOKIE=

# Favorite team(s) to always download even if outside the top 2 matches:
FAVORITE_TEAMS=Kansas City Chiefs, KC, Chiefs

# qBittorrent Connection (API Key recommended for v5.2.0+)
QBIT_HOST=http://host.docker.internal:8080
QBIT_API_KEY=qbt_your_api_key_here
# Or username/password fallback:
# QBIT_USERNAME=admin
# QBIT_PASSWORD=adminadmin
QBIT_CATEGORY=NFL
QBIT_TAGS=720pier,thuuz
QBIT_SAVE_PATH=/nfl

# Service Schedule (Mon, Tue, Wed at 9 AM, 3 PM, and 9 PM)
SCHEDULE_DAYS=Mon,Tue,Wed
SCHEDULE_TIMES=09:00,15:00,21:00
RUN_ON_STARTUP=false
RENAME_TORRENT_FILES=true
```

### 2. Networking with Existing qBittorrent Container

- **Option A (Host Gateway / LAN Port - No file changes needed)**:
  If your qBittorrent container maps its port (e.g., `-p 8087:8087` or `-p 8080:8080`), you don't need any network changes:
  ```ini
  QBIT_HOST=http://host.docker.internal:8087
  # or use your server LAN IP:
  # QBIT_HOST=http://192.168.1.100:8087
  ```

- **Option B (Shared Docker Network via Override - Clean Git Pulls)**:
  To join your existing qBittorrent container network **without modifying `docker-compose.yml`** (which prevents `git pull` conflicts):
  ```bash
  cp docker-compose.override.yml.example docker-compose.override.yml
  ```
  Edit `docker-compose.override.yml` and set the name of your qBittorrent network. Docker Compose will automatically load and merge this file!
  Then set in `.env`:
  ```ini
  QBIT_HOST=http://<qbittorrent_container_name>:8080
  ```

### 3. Launch with Docker Compose

Build and run in the background:

```bash
docker compose up -d --build
```

View live logs:

```bash
docker compose logs -f
```

---

## Local Development (Python Virtual Environment)

If you wish to run or test the script locally without Docker:

### 1. Activate the Virtual Environment

```bash
source venv/bin/activate
```

*(Or create a new one if setting up on another machine: `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`)*

### 2. Run in One-Shot Mode

Test a single check cycle without starting the scheduler:

```bash
python main.py --once
```

### 3. Run the Scheduler Service

Run the service with recurring checks:

```bash
python main.py
```

### 4. Run Tests

Execute the unit test suite:

```bash
python -m unittest discover tests
```

---

## State Persistence (SQLite Database)

All downloaded torrent history, execution runs, and metadata are saved to an embedded SQLite database at `data/nfl_grabber.db` (configured via `DB_PATH`):

- **`processed_torrents` table**: Tracks all added torrent URLs, titles, weeks, ratings, and timestamps to prevent duplicates.
- **`runs` table**: Logs execution history with start/completion times, status (`SUCCESS`, `NO_TORRENTS`, `ERROR`), torrents added count, and next scheduled run time.
- **`metadata` table**: Stores key-value state such as `last_processed_week` and `next_run_at`.
- **Automatic Migration**: On startup, if a legacy `data/state.json` file is present, records are automatically migrated into SQLite and backed up as `data/state.json.bak`.

When deployed in Docker, the `./data` directory is mounted as a volume (`./data:/app/data`), preserving your database and state across container rebuilds and updates.
