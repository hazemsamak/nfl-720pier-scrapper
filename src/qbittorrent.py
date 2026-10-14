import logging
from typing import Optional
import qbittorrentapi

logger = logging.getLogger(__name__)


class QBittorrentClient:
    """Interacts with qBittorrent Web API to send torrents."""

    def __init__(
        self,
        host: str,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        category: str = "NFL",
        tags: str = "720pier,thuuz",
        save_path: Optional[str] = "/nfl",
        auto_start: bool = True,
    ):
        self.host = host.rstrip("/")
        self.api_key = api_key.strip() if api_key else None
        self.username = username
        self.password = password
        self.category = category
        self.tags = tags
        self.save_path = save_path
        self.auto_start = auto_start
        self._client: Optional[qbittorrentapi.Client] = None

    def get_client(self) -> qbittorrentapi.Client:
        if self._client is None:
            if self.api_key:
                token = self.api_key if self.api_key.startswith("Bearer ") else f"Bearer {self.api_key}"
                logger.info("Connecting to qBittorrent at %s using API Key...", self.host)
                self._client = qbittorrentapi.Client(
                    host=self.host,
                    EXTRA_HEADERS={"Authorization": token},
                    REQUESTS_ARGS={"timeout": (10, 30)},
                )
                try:
                    logger.info(
                        "Connected to qBittorrent successfully via API key (API v%s, App v%s)",
                        getattr(self._client.app, "web_api_version", "unknown"),
                        getattr(self._client.app, "version", "unknown"),
                    )
                except Exception as e:
                    logger.error("Could not reach or authenticate with qBittorrent using API key at %s: %s", self.host, e)
                    raise
            else:
                user = self.username or "admin"
                logger.info("Connecting to qBittorrent at %s as '%s'...", self.host, user)
                self._client = qbittorrentapi.Client(
                    host=self.host,
                    username=user,
                    password=self.password or "",
                    REQUESTS_ARGS={"timeout": (10, 30)},
                )
                try:
                    self._client.auth_log_in()
                    logger.info(
                        "Connected to qBittorrent successfully (API v%s, App v%s)",
                        getattr(self._client.app, "web_api_version", "unknown"),
                        getattr(self._client.app, "version", "unknown"),
                    )
                except qbittorrentapi.LoginFailed as e:
                    logger.error("qBittorrent login failed: %s", e)
                    raise
                except Exception as e:
                    logger.error("Could not reach qBittorrent at %s: %s", self.host, e)
                    raise
        return self._client

    def test_connection(self) -> bool:
        try:
            client = self.get_client()
            if self.api_key:
                _ = client.app.version
                return True
            return client.auth.is_logged_in
        except Exception as e:
            logger.warning("qBittorrent connection test failed: %s", e)
            return False

    def rename_torrent_content(self, torrent_hash: str, custom_name: str, max_retries: int = 6) -> bool:
        """
        Renames the underlying media file on disk and the torrent display name in qBittorrent.
        custom_name: e.g. 'Week 05 - Buffalo Bills at Los Angeles Rams (2026)'
        """
        import os
        import time
        try:
            client = self.get_client()
            clean_name = os.path.splitext(custom_name)[0]

            for _ in range(max_retries):
                files = client.torrents_files(torrent_hash=torrent_hash)
                if files:
                    old_file_name = files[0].name
                    _, ext = os.path.splitext(old_file_name)
                    new_file_name = f"{clean_name}{ext or '.mkv'}"

                    if old_file_name != new_file_name:
                        client.torrents_rename_file(
                            torrent_hash=torrent_hash,
                            old_path=old_file_name,
                            new_path=new_file_name,
                        )
                    client.torrents_rename(
                        torrent_hash=torrent_hash,
                        new_torrent_name=clean_name,
                    )
                    logger.info("Renamed torrent and content file in qBittorrent: '%s'", new_file_name)
                    return True
                time.sleep(0.5)

            logger.warning("Could not rename torrent %s: no files found within timeout", torrent_hash)
            return False
        except Exception as e:
            logger.warning("Failed to rename torrent content for %s: %s", torrent_hash, e)
            return False

    def add_torrent_file(
        self,
        torrent_content: bytes,
        filename: str,
        custom_name: Optional[str] = None,
    ) -> bool:
        """Uploads .torrent binary data to qBittorrent and optionally renames the content file."""
        try:
            client = self.get_client()
            # Ensure category exists and has configured save_path
            if self.category:
                try:
                    client.torrents_create_category(name=self.category, save_path=self.save_path)
                except qbittorrentapi.Conflict409Error:
                    if self.save_path:
                        try:
                            client.torrents_edit_category(name=self.category, save_path=self.save_path)
                        except Exception as e:
                            logger.debug("Category edit notice: %s", e)
                except Exception as e:
                    logger.debug("Category creation notice: %s", e)

            is_paused = not self.auto_start

            result = client.torrents_add(
                torrent_files={filename: torrent_content},
                save_path=self.save_path,
                category=self.category,
                tags=self.tags,
                is_paused=is_paused,
            )
            logger.info("Torrent '%s' sent to qBittorrent (save_path=%s). API Response: %s", filename, self.save_path, result)

            if custom_name:
                torrent_hash = None
                if hasattr(result, "added_torrent_ids") and result.added_torrent_ids:
                    torrent_hash = result.added_torrent_ids[0]
                if torrent_hash:
                    self.rename_torrent_content(torrent_hash, custom_name)

            return True
        except Exception as e:
            logger.error("Failed to add torrent file '%s' to qBittorrent: %s", filename, e)
            return False

    def add_torrent_url(self, torrent_url: str) -> bool:
        """Adds torrent via URL."""
        try:
            client = self.get_client()
            if self.category:
                try:
                    client.torrents_create_category(name=self.category, save_path=self.save_path)
                except qbittorrentapi.Conflict409Error:
                    if self.save_path:
                        try:
                            client.torrents_edit_category(name=self.category, save_path=self.save_path)
                        except Exception as e:
                            logger.debug("Category edit notice: %s", e)
                except Exception as e:
                    logger.debug("Category creation notice: %s", e)

            is_paused = not self.auto_start

            result = client.torrents_add(
                urls=torrent_url,
                save_path=self.save_path,
                category=self.category,
                tags=self.tags,
                is_paused=is_paused,
            )
            logger.info("Torrent URL sent to qBittorrent: %s (save_path=%s). Response: %s", torrent_url, self.save_path, result)
            return True
        except Exception as e:
            logger.error("Failed to add torrent URL '%s' to qBittorrent: %s", torrent_url, e)
            return False
