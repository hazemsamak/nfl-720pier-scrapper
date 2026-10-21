import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/nfl_grabber.db"
DEFAULT_LEGACY_JSON_PATH = "data/state.json"


class DatabaseManager:
    """
    Manages persistent state using an embedded SQLite database.
    Stores processed torrents, run execution history, and application metadata.
    Automatically migrates legacy data/state.json if present.
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        legacy_json_path: Optional[str] = DEFAULT_LEGACY_JSON_PATH,
    ):
        # If passed path ends with .json, adapt to database path and use json as legacy path
        if db_path.endswith(".json"):
            self.legacy_json_path = db_path
            dirname = os.path.dirname(db_path) or "data"
            self.db_path = os.path.join(dirname, "nfl_grabber.db")
        else:
            self.db_path = db_path
            self.legacy_json_path = legacy_json_path

        # Ensure database directory exists
        dirname = os.path.dirname(self.db_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate_legacy_json()

    def _init_schema(self) -> None:
        """Initializes tables and indices."""
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed_torrents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    torrent_url TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    week TEXT NOT NULL,
                    rating TEXT,
                    added_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_torrents_url ON processed_torrents(torrent_url);

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    week_title TEXT,
                    torrents_added_count INTEGER DEFAULT 0,
                    next_run_at TEXT,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                );
            """)

    def _migrate_legacy_json(self) -> None:
        """Imports existing records from legacy state.json into SQLite if not already migrated."""
        if not self.legacy_json_path or not os.path.exists(self.legacy_json_path):
            return

        # Check if already migrated
        migrated = self.get_metadata("migrated_from_json")
        if migrated:
            return

        try:
            with open(self.legacy_json_path, "r", encoding="utf-8") as f:
                legacy_data = json.load(f)

            if isinstance(legacy_data, dict):
                torrents = legacy_data.get("processed_torrents", [])
                imported_count = 0
                with self._conn:
                    for t in torrents:
                        t_id = t.get("id") or t.get("torrent_url")
                        if not t_id:
                            continue
                        self._conn.execute(
                            """
                            INSERT OR IGNORE INTO processed_torrents (torrent_url, title, week, added_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                t_id,
                                t.get("title", "Unknown"),
                                t.get("week", "Unknown"),
                                t.get("added_at", datetime.now(timezone.utc).isoformat()),
                            ),
                        )
                        imported_count += 1

                    last_week = legacy_data.get("last_processed_week")
                    if last_week:
                        self.set_metadata("last_processed_week", str(last_week))

                    last_run = legacy_data.get("last_run_timestamp")
                    if last_run:
                        self.set_metadata("last_run_timestamp", str(last_run))

                    now_str = datetime.now(timezone.utc).isoformat()
                    self.set_metadata("migrated_from_json", now_str)

                # Create backup of the legacy json file
                bak_path = f"{self.legacy_json_path}.bak"
                shutil.copy2(self.legacy_json_path, bak_path)
                logger.info(
                    "Successfully migrated %d torrent records from %s to SQLite (%s). Backup created at %s.",
                    imported_count,
                    self.legacy_json_path,
                    self.db_path,
                    bak_path,
                )
        except Exception as e:
            logger.error("Error migrating legacy state.json: %s", e)

    # --------------------------------------------------------------------------
    # Torrent Tracking
    # --------------------------------------------------------------------------

    def is_torrent_processed(self, torrent_id: str) -> bool:
        """Checks if a torrent URL/ID has already been recorded."""
        cur = self._conn.execute(
            "SELECT 1 FROM processed_torrents WHERE torrent_url = ? LIMIT 1",
            (torrent_id,),
        )
        return cur.fetchone() is not None

    def record_processed_torrent(
        self,
        torrent_id: str,
        title: str,
        week: str,
        rating: Optional[str] = None,
    ) -> bool:
        """
        Records a newly processed torrent.
        Returns True if inserted, False if it was already processed.
        If already processed but rating is missing, updates the rating.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with self._conn:
                cur = self._conn.execute(
                    """
                    INSERT INTO processed_torrents (torrent_url, title, week, rating, added_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (torrent_id, title, week, rating, now_str),
                )
                self.set_metadata("last_processed_week", week)
                self.set_metadata("last_run_timestamp", now_str)
                return cur.rowcount > 0
        except sqlite3.IntegrityError:
            if rating:
                with self._conn:
                    self._conn.execute(
                        """
                        UPDATE processed_torrents
                        SET rating = ?
                        WHERE torrent_url = ? AND (rating IS NULL OR rating = '' OR rating = 'N/A')
                        """,
                        (str(rating), torrent_id),
                    )
            return False

    def update_torrent_rating(self, torrent_id: str, rating: str) -> bool:
        """Updates the rating for a torrent if it is currently empty or missing."""
        if not rating or rating == "N/A":
            return False
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE processed_torrents
                SET rating = ?
                WHERE (torrent_url = ? OR id = ?) AND (rating IS NULL OR rating = '' OR rating = 'N/A')
                """,
                (str(rating), str(torrent_id), int(torrent_id) if str(torrent_id).isdigit() else -1),
            )
            return cur.rowcount > 0

    def get_processed_count(self) -> int:
        """Returns total number of processed torrents."""
        cur = self._conn.execute("SELECT COUNT(*) FROM processed_torrents")
        row = cur.fetchone()
        return row[0] if row else 0

    def get_all_torrents(self) -> List[Dict[str, Any]]:
        """Returns list of all processed torrent dictionaries."""
        cur = self._conn.execute(
            "SELECT id, torrent_url, title, week, rating, added_at FROM processed_torrents ORDER BY id DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_processed_torrents(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Returns paginated processed torrents ordered by most recent first."""
        cur = self._conn.execute(
            """
            SELECT id, torrent_url, title, week, rating, added_at
            FROM processed_torrents
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in cur.fetchall()]

    def delete_processed_torrent(self, identifier: str) -> bool:
        """
        Deletes a processed torrent by database id or torrent_url.
        Returns True if a record was removed, False otherwise.
        """
        with self._conn:
            if str(identifier).isdigit():
                cur = self._conn.execute("DELETE FROM processed_torrents WHERE id = ?", (int(identifier),))
                if cur.rowcount > 0:
                    return True
            cur = self._conn.execute("DELETE FROM processed_torrents WHERE torrent_url = ?", (str(identifier),))
            return cur.rowcount > 0

    def delete_processed_torrents_by_week(self, week: str) -> int:
        """Deletes all processed torrents for a given week. Returns count of removed rows."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM processed_torrents WHERE week = ? OR week LIKE ?", (week, f"%{week}%"))
            return cur.rowcount

    # --------------------------------------------------------------------------
    # Run Execution History
    # --------------------------------------------------------------------------

    def record_run_start(self, week_title: Optional[str] = None) -> int:
        """Creates a new run entry with status RUNNING and returns the run_id."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO runs (started_at, status, week_title)
                VALUES (?, ?, ?)
                """,
                (now_str, "RUNNING", week_title),
            )
            return cur.lastrowid

    def record_run_completion(
        self,
        run_id: int,
        status: str,
        week_title: Optional[str] = None,
        torrents_added_count: int = 0,
        next_run_at: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Updates run entry with completion status, metrics, and next scheduled run."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                UPDATE runs
                SET completed_at = ?,
                    status = ?,
                    week_title = COALESCE(?, week_title),
                    torrents_added_count = ?,
                    next_run_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (now_str, status, week_title, torrents_added_count, next_run_at, error_message, run_id),
            )
            self.set_metadata("last_run_status", status)
            if next_run_at:
                self.set_metadata("next_run_at", next_run_at)

    def get_recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetches the most recent execution runs."""
        cur = self._conn.execute(
            """
            SELECT id, started_at, completed_at, status, week_title, torrents_added_count, next_run_at, error_message
            FROM runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    # --------------------------------------------------------------------------
    # Metadata Key-Value Store
    # --------------------------------------------------------------------------

    def set_metadata(self, key: str, value: str) -> None:
        """Stores a key-value pair in metadata."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO metadata (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now_str),
            )

    def get_metadata(self, key: str) -> Optional[str]:
        """Retrieves a metadata value by key."""
        cur = self._conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def get_last_processed_week(self) -> Optional[str]:
        return self.get_metadata("last_processed_week")

    def get_next_run_at(self) -> Optional[str]:
        return self.get_metadata("next_run_at")

    # --------------------------------------------------------------------------
    # Backward Compatibility
    # --------------------------------------------------------------------------

    @property
    def data(self) -> Dict[str, Any]:
        """Backward compatible dict representation matching old StateManager."""
        return {
            "last_processed_week": self.get_last_processed_week(),
            "last_run_timestamp": self.get_metadata("last_run_timestamp"),
            "processed_torrents": [
                {
                    "id": t["torrent_url"],
                    "title": t["title"],
                    "week": t["week"],
                    "added_at": t["added_at"],
                }
                for t in self.get_all_torrents()
            ],
        }

    def close(self) -> None:
        """Closes the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass
