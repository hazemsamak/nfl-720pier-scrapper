import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StateManager:
    """Manages persistent state of processed weeks and torrents."""

    def __init__(self, state_file_path: str = "data/state.json"):
        self.state_file_path = state_file_path
        self.data: Dict[str, Any] = {
            "last_processed_week": None,
            "last_run_timestamp": None,
            "processed_torrents": [],  # List of {id, title, week, added_at}
        }
        self.load()

    def load(self) -> None:
        if os.path.exists(self.state_file_path):
            try:
                with open(self.state_file_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
                        logger.info("Loaded state from %s (%d processed torrents)",
                                    self.state_file_path, len(self.data.get("processed_torrents", [])))
            except Exception as e:
                logger.error("Failed to read state file %s: %s. Starting fresh.", self.state_file_path, e)
        else:
            logger.info("No state file found at %s. A new one will be created.", self.state_file_path)

    def save(self) -> None:
        try:
            dirname = os.path.dirname(self.state_file_path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(self.state_file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            logger.debug("Saved state to %s", self.state_file_path)
        except Exception as e:
            logger.error("Failed to save state to %s: %s", self.state_file_path, e)

    def is_torrent_processed(self, torrent_id: str) -> bool:
        """Checks if a torrent ID/URL has already been processed."""
        processed = self.data.get("processed_torrents", [])
        return any(entry.get("id") == torrent_id for entry in processed)

    def record_processed_torrent(self, torrent_id: str, title: str, week: str) -> None:
        """Records a newly added torrent and updates state."""
        if not self.is_torrent_processed(torrent_id):
            self.data.setdefault("processed_torrents", []).append({
                "id": torrent_id,
                "title": title,
                "week": week,
                "added_at": datetime.utcnow().isoformat() + "Z",
            })
            self.data["last_processed_week"] = week
            self.data["last_run_timestamp"] = datetime.utcnow().isoformat() + "Z"
            self.save()

    def get_processed_count(self) -> int:
        return len(self.data.get("processed_torrents", []))
