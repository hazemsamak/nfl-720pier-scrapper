import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from bs4 import BeautifulSoup

from src.config import Config
from src.notifier import Notifier
from src.qbittorrent import QBittorrentClient
from src.scraper import PierScraper
from src.state import StateManager


class TestNFLGrabber(unittest.TestCase):

    def test_state_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            mgr = StateManager(state_path)

            self.assertFalse(mgr.is_torrent_processed("https://720pier.ru/download/file.php?id=101"))
            self.assertEqual(mgr.get_processed_count(), 0)

            mgr.record_processed_torrent(
                torrent_id="https://720pier.ru/download/file.php?id=101",
                title="Kansas City Chiefs vs Denver Broncos",
                week="Week 5",
            )

            self.assertTrue(mgr.is_torrent_processed("https://720pier.ru/download/file.php?id=101"))
            self.assertEqual(mgr.get_processed_count(), 1)
            self.assertEqual(mgr.data["last_processed_week"], "Week 5")

            # Reload state from disk
            mgr_reloaded = StateManager(state_path)
            self.assertTrue(mgr_reloaded.is_torrent_processed("https://720pier.ru/download/file.php?id=101"))
            self.assertEqual(mgr_reloaded.get_processed_count(), 1)

    def test_scraper_find_latest_week(self):
        sample_html = """
        <html>
        <body>
            <div class="postbody">
                <div class="spoiler">
                    <strong>Week 1</strong>
                    <a href="viewtopic.php?t=1001">Match 1</a>
                </div>
                <div class="spoiler">
                    <strong>Week 4</strong>
                    <a href="viewtopic.php?t=1004">Match 4</a>
                </div>
                <div class="spoiler">
                    <strong>Week 5</strong>
                    <div class="content">
                        [100] <a href="viewtopic.php?t=5001">KC Chiefs @ Baltimore Ravens</a><br/>
                        [96] <a href="download/file.php?id=5002">Buffalo Bills @ Miami Dolphins</a><br/>
                        [85] <a href="download/file.php?id=5003">Detroit Lions @ Green Bay Packers</a><br/>
                    </div>
                </div>
                <div class="spoiler">
                    <strong>Week 2</strong>
                    <a href="viewtopic.php?t=1002">Match 2</a>
                </div>
            </div>
        </body>
        </html>
        """
        scraper = PierScraper(thread_url="https://720pier.ru/viewtopic.php?t=76272")
        soup = BeautifulSoup(sample_html, "lxml")
        week_title, week_elem = scraper.find_latest_week_section(soup)

        self.assertEqual(week_title, "Week 5")
        self.assertIn("KC Chiefs", week_elem.get_text())

    def test_scraper_extract_top_two_torrents_direct(self):
        sample_week_html = """
        <div class="spoiler">
            <strong>Week 5</strong>
            <div class="content">
                100 pts - <a href="download/file.php?id=5001">KC Chiefs vs Baltimore Ravens (1080p)</a><br/>
                96 pts - <a href="download/file.php?id=5002">Buffalo Bills vs Miami Dolphins</a><br/>
                85 pts - <a href="download/file.php?id=5003">Detroit Lions vs Green Bay Packers</a><br/>
                72 pts - <a href="download/file.php?id=5004">Philadelphia Eagles vs Dallas Cowboys</a><br/>
            </div>
        </div>
        """
        scraper = PierScraper(thread_url="https://720pier.ru/viewtopic.php?t=76272")
        soup = BeautifulSoup(sample_week_html, "lxml")
        top_links = scraper.extract_top_torrent_links(soup, limit=2)

        self.assertEqual(len(top_links), 2)
        self.assertIn("5001", top_links[0]["torrent_url"])
        self.assertIn("5002", top_links[1]["torrent_url"])
        self.assertEqual(top_links[0]["rating"], "100")
        self.assertEqual(top_links[1]["rating"], "96")

    def test_scraper_resolves_game_topic_page(self):
        sample_week_html = """
        <div class="spoiler">
            <strong>Week 5</strong>
            <div class="content">
                98 pts - <a href="viewtopic.php?t=9001">KC Chiefs vs Denver Broncos</a><br/>
                94 pts - <a href="download/file.php?id=9002">Buffalo Bills vs Jets</a><br/>
            </div>
        </div>
        """
        game_topic_html = """
        <html>
        <head><title>NFL Week 5 - KC Chiefs vs Denver Broncos 1080p</title></head>
        <body>
            <h2 class="topic-title">KC Chiefs vs Denver Broncos</h2>
            <div class="content">
                <a href="download/file.php?id=9999">Download 1080p 60fps Torrent</a>
            </div>
        </body>
        </html>
        """
        scraper = PierScraper(thread_url="https://720pier.ru/viewtopic.php?t=76272")

        # Mock the request to viewtopic.php?t=9001
        with patch.object(scraper.session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = game_topic_html
            mock_get.return_value = mock_resp

            soup = BeautifulSoup(sample_week_html, "lxml")
            top_links = scraper.extract_top_torrent_links(soup, limit=2)

            self.assertEqual(len(top_links), 2)
            self.assertIn("id=9999", top_links[0]["torrent_url"])
            self.assertEqual(top_links[0]["rating"], "98")
            self.assertEqual(top_links[0]["source"], "game_topic")

    def test_matches_favorite_team(self):
        scraper = PierScraper(thread_url="https://720pier.ru/viewtopic.php?t=76272")
        teams = ["Kansas City Chiefs", "KC", "Chiefs"]

        self.assertTrue(scraper.matches_favorite_team("Denver Broncos at Kansas City Chiefs", teams))
        self.assertTrue(scraper.matches_favorite_team("KC Chiefs vs Las Vegas Raiders", teams))
        self.assertTrue(scraper.matches_favorite_team("KC @ LAC", teams))
        self.assertTrue(scraper.matches_favorite_team("Kansas City at Miami", teams))
        self.assertTrue(scraper.matches_favorite_team("Chiefs vs Bills", teams))

        self.assertFalse(scraper.matches_favorite_team("Buffalo Bills at Los Angeles Rams", teams))
        self.assertFalse(scraper.matches_favorite_team("San Francisco 49ers at Seattle Seahawks", teams))
        self.assertFalse(scraper.matches_favorite_team("Philadelphia Eagles at Jacksonville Jaguars", teams))

    def test_scraper_extracts_favorite_team_outside_top_limit(self):
        sample_week_html = """
        <div class="spoiler">
            <strong>Week 4</strong>
            <div class="content">
                99 pts - <a href="download/file.php?id=4001">Dallas Cowboys at Houston Texans</a><br/>
                97 pts - <a href="download/file.php?id=4002">New England Patriots at Buffalo Bills</a><br/>
                97 pts - <a href="download/file.php?id=4003">Los Angeles Rams at Philadelphia Eagles</a><br/>
                94 pts - <a href="download/file.php?id=4004">Detroit Lions at Carolina Panthers</a><br/>
                93 pts - <a href="download/file.php?id=4005">Kansas City Chiefs at Las Vegas Raiders</a><br/>
                77 pts - <a href="download/file.php?id=4006">Jacksonville Jaguars at Cincinnati Bengals</a><br/>
            </div>
        </div>
        """
        scraper = PierScraper(thread_url="https://720pier.ru/viewtopic.php?t=76272")
        soup = BeautifulSoup(sample_week_html, "lxml")
        results = scraper.extract_top_torrent_links(
            soup,
            limit=2,
            favorite_teams=["Kansas City Chiefs", "KC", "Chiefs"],
        )

        # Should extract top 2 (4001, 4002) PLUS the favorite team match at #5 (4005) = 3 total
        self.assertEqual(len(results), 3)
        self.assertIn("4001", results[0]["torrent_url"])
        self.assertIn("4002", results[1]["torrent_url"])
        self.assertIn("4005", results[2]["torrent_url"])
        self.assertEqual(results[0]["selection_reason"], "top")
        self.assertEqual(results[1]["selection_reason"], "top")
        self.assertEqual(results[2]["selection_reason"], "favorite")
        self.assertTrue(results[2]["is_favorite_team"])

    def test_notifier_formatting(self):
        notifier = Notifier(discord_webhook_url="https://discord.com/api/webhooks/mock")
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 204
            notifier.notify_torrents_added("Week 5", [
                {"title": "Chiefs vs Ravens", "rating": "100"},
                {"title": "Bills vs Dolphins", "rating": "96"},
            ])
            self.assertTrue(mock_post.called)
            call_kwargs = mock_post.call_args[1]
            self.assertIn("Chiefs vs Ravens", call_kwargs["json"]["content"])
            self.assertIn("[100/100]", call_kwargs["json"]["content"])

    def test_config_loader(self):
        config = Config.load()
        self.assertIsNotNone(config.pier_thread_url)
        self.assertIsNotNone(config.qbit_host)
        self.assertEqual(config.qbit_save_path, "/nfl")
        self.assertEqual(config.schedule_days, ["Mon", "Tue", "Wed"])
        self.assertIsInstance(config.check_interval_hours, float)

    def test_is_scheduled_day(self):
        from datetime import datetime
        from src.main import is_scheduled_day

        active_days = ["Mon", "Tue", "Wed"]

        # Monday 2026-10-12
        self.assertTrue(is_scheduled_day(active_days, datetime(2026, 10, 12, 10, 0)))
        # Tuesday 2026-10-13
        self.assertTrue(is_scheduled_day(active_days, datetime(2026, 10, 13, 15, 30)))
        # Wednesday 2026-10-14
        self.assertTrue(is_scheduled_day(active_days, datetime(2026, 10, 14, 23, 59)))

        # Thursday 2026-10-15
        self.assertFalse(is_scheduled_day(active_days, datetime(2026, 10, 15, 8, 0)))
        # Friday 2026-10-16
        self.assertFalse(is_scheduled_day(active_days, datetime(2026, 10, 16, 12, 0)))
        # Saturday 2026-10-17
        self.assertFalse(is_scheduled_day(active_days, datetime(2026, 10, 17, 18, 0)))
        # Sunday 2026-10-18
        self.assertFalse(is_scheduled_day(active_days, datetime(2026, 10, 18, 20, 0)))

        # Empty / None means every day
        self.assertTrue(is_scheduled_day([], datetime(2026, 10, 15)))
        self.assertTrue(is_scheduled_day(None, datetime(2026, 10, 16)))

    def test_qbittorrent_client_save_path(self):
        from src.qbittorrent import QBittorrentClient
        client = QBittorrentClient(
            host="http://localhost:8080",
            username="admin",
            password="password",
            category="NFL",
            tags="720pier,thuuz",
            save_path="/nfl",
            auto_start=True,
        )
        mock_api = MagicMock()
        with patch.object(client, "get_client", return_value=mock_api):
            success = client.add_torrent_file(b"dummy_content", "match.torrent")
            self.assertTrue(success)
            mock_api.torrents_create_category.assert_called_once_with(name="NFL", save_path="/nfl")
            mock_api.torrents_add.assert_called_once_with(
                torrent_files={"match.torrent": b"dummy_content"},
                save_path="/nfl",
                category="NFL",
                tags="720pier,thuuz",
                is_paused=False,
            )

    def test_qbittorrent_client_api_key(self):
        from src.qbittorrent import QBittorrentClient
        client = QBittorrentClient(
            host="http://localhost:8080",
            api_key="qbt_test1234567890",
            category="NFL",
            save_path="/nfl",
        )
        with patch("qbittorrentapi.Client") as mock_client_cls:
            mock_instance = MagicMock()
            mock_instance.app.version = "v5.2.3"
            mock_instance.app.web_api_version = "2.15.1"
            mock_client_cls.return_value = mock_instance

            # Test get_client
            api = client.get_client()
            self.assertEqual(api, mock_instance)
            mock_client_cls.assert_called_once_with(
                host="http://localhost:8080",
                EXTRA_HEADERS={"Authorization": "Bearer qbt_test1234567890"},
                REQUESTS_ARGS={"timeout": (10, 30)},
            )
            # auth_log_in should NOT be called when using API key
            self.assertFalse(mock_instance.auth_log_in.called)

            # Test connection test
            self.assertTrue(client.test_connection())

    def test_format_match_name(self):
        from src.main import format_match_name
        t = {
            "matchup": "Buffalo Bills at Los Angeles Rams",
            "title": "NFL 2026-2027 / Week 05 / 12.10.2026 / Buffalo Bills @ Los Angeles Rams [Football, WEB-DL HD/720p/60fps, MKV/H.264, EN/ESPN]",
        }
        res = format_match_name("Week 5", t)
        self.assertEqual(res, "Week 05 - Buffalo Bills at Los Angeles Rams (2026)")

        # Test with @ in matchup
        t2 = {
            "matchup": "KC Chiefs @ Denver Broncos",
            "title": "NFL 2026-2027 / Week 01 / 08.09.2026 / KC Chiefs @ Denver Broncos [1080p]",
        }
        res2 = format_match_name("Week 1", t2)
        self.assertEqual(res2, "Week 01 - KC Chiefs at Denver Broncos (2026)")

    def test_qbittorrent_rename_content(self):
        from src.qbittorrent import QBittorrentClient
        client = QBittorrentClient(host="http://localhost:8080", api_key="qbt_test")
        mock_api = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "NFL-2026-10-12.W05_BUF@LAR.mkv"
        mock_api.torrents_files.return_value = [mock_file]

        with patch.object(client, "get_client", return_value=mock_api):
            success = client.rename_torrent_content(
                torrent_hash="mock_hash_123",
                custom_name="Week 05 - Buffalo Bills at Los Angeles Rams (2026)",
            )
            self.assertTrue(success)
            mock_api.torrents_rename_file.assert_called_once_with(
                torrent_hash="mock_hash_123",
                old_path="NFL-2026-10-12.W05_BUF@LAR.mkv",
                new_path="Week 05 - Buffalo Bills at Los Angeles Rams (2026).mkv",
            )
            mock_api.torrents_rename.assert_called_once_with(
                torrent_hash="mock_hash_123",
                new_torrent_name="Week 05 - Buffalo Bills at Los Angeles Rams (2026)",
            )

    def test_qbittorrent_rename_content_retry_on_404(self):
        import qbittorrentapi
        from src.qbittorrent import QBittorrentClient
        client = QBittorrentClient(host="http://localhost:8080", api_key="qbt_test")
        mock_api = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "NFL-2026-10-18.W06_ARI@LAR.mkv"

        # First call raises NotFound404Error (simulating qBittorrent asynchronous registration)
        # Second call returns the files list
        mock_api.torrents_files.side_effect = [
            qbittorrentapi.NotFound404Error("Torrent hash(es): mock_hash_456"),
            [mock_file],
        ]

        with patch.object(client, "get_client", return_value=mock_api):
            success = client.rename_torrent_content(
                torrent_hash="mock_hash_456",
                custom_name="Week 06 - Arizona Cardinals at Los Angeles Rams (2026)",
                max_retries=3,
                delay_seconds=0.01,
            )
            self.assertTrue(success)
            self.assertEqual(mock_api.torrents_files.call_count, 2)
            mock_api.torrents_rename_file.assert_called_once_with(
                torrent_hash="mock_hash_456",
                old_path="NFL-2026-10-18.W06_ARI@LAR.mkv",
                new_path="Week 06 - Arizona Cardinals at Los Angeles Rams (2026).mkv",
            )
            mock_api.torrents_rename.assert_called_once_with(
                torrent_hash="mock_hash_456",
                new_torrent_name="Week 06 - Arizona Cardinals at Los Angeles Rams (2026)",
            )

    def test_qbittorrent_rename_content_exhausts_retries(self):
        import qbittorrentapi
        from src.qbittorrent import QBittorrentClient
        client = QBittorrentClient(host="http://localhost:8080", api_key="qbt_test")
        mock_api = MagicMock()
        mock_api.torrents_files.side_effect = qbittorrentapi.NotFound404Error("Torrent hash(es): mock_hash_456")

        with patch.object(client, "get_client", return_value=mock_api):
            success = client.rename_torrent_content(
                torrent_hash="mock_hash_456",
                custom_name="Week 06 - Arizona Cardinals at Los Angeles Rams (2026)",
                max_retries=2,
                delay_seconds=0.01,
            )
            self.assertFalse(success)
            self.assertEqual(mock_api.torrents_files.call_count, 2)


    def test_notifier_error_discord_embed(self):
        notifier = Notifier(discord_webhook_url="https://discord.com/api/webhooks/mock")
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 204
            dispatched = notifier.notify_error(
                title="720pier Authentication Failure",
                details="HTTP 403 Forbidden: Session cookie has expired.",
                suggestion="Update PIER_COOKIE in .env.",
                context={"URL": "https://720pier.ru"},
            )
            self.assertTrue(dispatched)
            self.assertTrue(mock_post.called)
            payload = mock_post.call_args[1]["json"]
            self.assertIn("content", payload)
            self.assertIn("embeds", payload)
            embed = payload["embeds"][0]
            self.assertIn("720pier Authentication Failure", embed["title"])
            self.assertEqual(embed["color"], 15158332)
            self.assertEqual(embed["description"], "HTTP 403 Forbidden: Session cookie has expired.")
            field_names = [f["name"] for f in embed["fields"]]
            self.assertIn("URL", field_names)
            self.assertIn("💡 Recommended Action", field_names)

    def test_notifier_error_cooldown(self):
        notifier = Notifier(discord_webhook_url="https://discord.com/api/webhooks/mock", cooldown_seconds=60)
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 204
            # First send: should dispatch
            res1 = notifier.notify_error(title="Test Error", details="Details A")
            self.assertTrue(res1)
            self.assertEqual(mock_post.call_count, 1)

            # Second send within cooldown: should throttle
            res2 = notifier.notify_error(title="Test Error", details="Details A")
            self.assertFalse(res2)
            self.assertEqual(mock_post.call_count, 1)

            # Third send with force=True: should bypass cooldown
            res3 = notifier.notify_error(title="Test Error", details="Details A", force=True)
            self.assertTrue(res3)
            self.assertEqual(mock_post.call_count, 2)

    def test_execute_grabber_cycle_qbit_auth_failure(self):
        from src.main import execute_grabber_cycle
        import qbittorrentapi

        config = MagicMock()
        config.qbit_host = "http://192.168.1.100:8087"
        config.qbit_api_key = "bad_key"
        config.qbit_username = None
        config.qbit_password = None
        config.qbit_category = "NFL"
        config.qbit_tags = "720pier"
        config.qbit_save_path = "/nfl"
        config.qbit_auto_start = True

        state = MagicMock()
        notifier = MagicMock()

        with patch("src.main.QBittorrentClient") as mock_qbit_cls:
            mock_qbit_inst = MagicMock()
            mock_qbit_inst.api_key = "bad_key"
            mock_qbit_inst.get_client.side_effect = qbittorrentapi.LoginFailed("Invalid credentials")
            mock_qbit_cls.return_value = mock_qbit_inst

            result = execute_grabber_cycle(config, state, notifier, dry_run=False)
            self.assertEqual(result, 0)
            notifier.notify_error.assert_called_once()
            call_kwargs = notifier.notify_error.call_args[1]
            self.assertEqual(call_kwargs["title"], "qBittorrent Authentication Failure")
            self.assertIn("Host", call_kwargs["context"])

    def test_execute_grabber_cycle_qbit_connection_failure(self):
        from src.main import execute_grabber_cycle

        config = MagicMock()
        config.qbit_host = "http://192.168.1.100:8087"
        config.qbit_api_key = "qbt_key"

        state = MagicMock()
        notifier = MagicMock()

        with patch("src.main.QBittorrentClient") as mock_qbit_cls:
            mock_qbit_inst = MagicMock()
            mock_qbit_inst.api_key = "qbt_key"
            mock_qbit_inst.get_client.side_effect = Exception("Connection refused on port 8087")
            mock_qbit_cls.return_value = mock_qbit_inst

            result = execute_grabber_cycle(config, state, notifier, dry_run=False)
            self.assertEqual(result, 0)
            notifier.notify_error.assert_called_once()
            call_kwargs = notifier.notify_error.call_args[1]
            self.assertEqual(call_kwargs["title"], "qBittorrent Connection Failure")

    def test_execute_grabber_cycle_pier_auth_failure(self):
        from src.main import execute_grabber_cycle

        config = MagicMock()
        config.pier_thread_url = "https://720pier.ru/viewtopic.php?t=76272"
        state = MagicMock()
        notifier = MagicMock()

        with patch("src.main.PierScraper") as mock_scraper_cls, \
             patch("src.main.QBittorrentClient") as mock_qbit_cls:
            mock_scraper = MagicMock()
            mock_scraper.fetch_thread_html.side_effect = Exception("HTTP 403 Forbidden: Access denied")
            mock_scraper_cls.return_value = mock_scraper

            mock_qbit = MagicMock()
            mock_qbit.api_key = "key"
            mock_qbit_cls.return_value = mock_qbit

            result = execute_grabber_cycle(config, state, notifier, dry_run=False)
            self.assertEqual(result, 0)
            notifier.notify_error.assert_called_once()
            call_kwargs = notifier.notify_error.call_args[1]
            self.assertEqual(call_kwargs["title"], "720pier Authentication Failure")
            self.assertIn("PIER_COOKIE", call_kwargs["suggestion"])

    def test_execute_grabber_cycle_pier_connection_failure(self):
        from src.main import execute_grabber_cycle

        config = MagicMock()
        config.pier_thread_url = "https://720pier.ru/viewtopic.php?t=76272"
        state = MagicMock()
        notifier = MagicMock()

        with patch("src.main.PierScraper") as mock_scraper_cls, \
             patch("src.main.QBittorrentClient") as mock_qbit_cls:
            mock_scraper = MagicMock()
            mock_scraper.fetch_thread_html.side_effect = Exception("Failed to establish a new connection: [Errno 61] Connection refused")
            mock_scraper_cls.return_value = mock_scraper

            mock_qbit = MagicMock()
            mock_qbit.api_key = "key"
            mock_qbit_cls.return_value = mock_qbit

            result = execute_grabber_cycle(config, state, notifier, dry_run=False)
            self.assertEqual(result, 0)
            notifier.notify_error.assert_called_once()
            call_kwargs = notifier.notify_error.call_args[1]
            self.assertEqual(call_kwargs["title"], "720pier Connection Failure")

    def test_execute_grabber_cycle_torrent_download_failure(self):
        from src.main import execute_grabber_cycle

        config = MagicMock()
        config.pier_thread_url = "https://720pier.ru/viewtopic.php?t=76272"
        config.rename_torrent_files = False
        config.favorite_teams = None
        config.check_interval_hours = 6.0
        config.schedule_days = ["Mon", "Tue", "Wed"]

        state = MagicMock()
        state.is_torrent_processed.return_value = False
        notifier = MagicMock()

        with patch("src.main.PierScraper") as mock_scraper_cls, \
             patch("src.main.QBittorrentClient") as mock_qbit_cls:
            mock_scraper = MagicMock()
            mock_scraper.fetch_thread_html.return_value = "<html><body><div>Week 5</div></body></html>"
            mock_scraper.find_latest_week_section.return_value = ("Week 5", MagicMock())
            mock_scraper.extract_top_torrent_links.return_value = [
                {"title": "Match 1", "rating": "100", "torrent_url": "https://720pier.ru/download/file.php?id=101"}
            ]
            mock_scraper.download_torrent.side_effect = Exception("Connection reset by peer")
            mock_scraper_cls.return_value = mock_scraper

            mock_qbit = MagicMock()
            mock_qbit.api_key = "key"
            mock_qbit_cls.return_value = mock_qbit

            result = execute_grabber_cycle(config, state, notifier, dry_run=False)
            self.assertEqual(result, 0)
            notifier.notify_error.assert_called_once()
            call_kwargs = notifier.notify_error.call_args[1]
            self.assertEqual(call_kwargs["title"], "Torrent Download Failure")

    def test_execute_grabber_cycle_qbit_forwarding_failure(self):
        from src.main import execute_grabber_cycle

        config = MagicMock()
        config.pier_thread_url = "https://720pier.ru/viewtopic.php?t=76272"
        config.rename_torrent_files = False
        config.favorite_teams = None
        config.qbit_save_path = "/nfl"
        config.check_interval_hours = 6.0
        config.schedule_days = ["Mon", "Tue", "Wed"]

        state = MagicMock()
        state.is_torrent_processed.return_value = False
        notifier = MagicMock()

        with patch("src.main.PierScraper") as mock_scraper_cls, \
             patch("src.main.QBittorrentClient") as mock_qbit_cls:
            mock_scraper = MagicMock()
            mock_scraper.fetch_thread_html.return_value = "<html><body><div>Week 5</div></body></html>"
            mock_scraper.find_latest_week_section.return_value = ("Week 5", MagicMock())
            mock_scraper.extract_top_torrent_links.return_value = [
                {"title": "Match 1", "rating": "100", "torrent_url": "https://720pier.ru/download/file.php?id=101"}
            ]
            mock_scraper.download_torrent.return_value = (b"dummy_bytes", "Match1.torrent")
            mock_scraper_cls.return_value = mock_scraper

            mock_qbit = MagicMock()
            mock_qbit.api_key = "key"
            mock_qbit.add_torrent_file.return_value = False  # qBittorrent rejects
            mock_qbit_cls.return_value = mock_qbit

            result = execute_grabber_cycle(config, state, notifier, dry_run=False)
            self.assertEqual(result, 0)
            notifier.notify_error.assert_called_once()
            call_kwargs = notifier.notify_error.call_args[1]
            self.assertEqual(call_kwargs["title"], "qBittorrent Forwarding Failure")


    def test_database_manager_operations(self):
        from src.database import DatabaseManager

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = DatabaseManager(db_path=db_path, legacy_json_path=None)

            self.assertFalse(db.is_torrent_processed("https://720pier.ru/download/101"))
            self.assertEqual(db.get_processed_count(), 0)

            # Insert new record
            inserted = db.record_processed_torrent(
                torrent_id="https://720pier.ru/download/101",
                title="Kansas City Chiefs at Denver Broncos",
                week="Week 5",
                rating="98",
            )
            self.assertTrue(inserted)
            self.assertTrue(db.is_torrent_processed("https://720pier.ru/download/101"))
            self.assertEqual(db.get_processed_count(), 1)
            self.assertEqual(db.get_last_processed_week(), "Week 5")

            # Duplicate insert should return False and not increment count
            duplicate = db.record_processed_torrent(
                torrent_id="https://720pier.ru/download/101",
                title="Kansas City Chiefs at Denver Broncos",
                week="Week 5",
            )
            self.assertFalse(duplicate)
            self.assertEqual(db.get_processed_count(), 1)

            db.close()

    def test_database_manager_migration_from_json(self):
        from src.database import DatabaseManager

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = os.path.join(tmpdir, "state.json")
            db_path = os.path.join(tmpdir, "migrated.db")

            # Create existing legacy state.json
            legacy_content = {
                "last_processed_week": "Week 4",
                "last_run_timestamp": "2026-10-10T12:00:00Z",
                "processed_torrents": [
                    {
                        "id": "https://720pier.ru/download/201",
                        "title": "Match 1",
                        "week": "Week 4",
                        "added_at": "2026-10-10T12:00:00Z",
                    },
                    {
                        "id": "https://720pier.ru/download/202",
                        "title": "Match 2",
                        "week": "Week 4",
                        "added_at": "2026-10-10T12:00:00Z",
                    },
                ],
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(legacy_content, f)

            # Initialize DatabaseManager pointing to this legacy json
            db = DatabaseManager(db_path=db_path, legacy_json_path=json_path)

            self.assertEqual(db.get_processed_count(), 2)
            self.assertTrue(db.is_torrent_processed("https://720pier.ru/download/201"))
            self.assertTrue(db.is_torrent_processed("https://720pier.ru/download/202"))
            self.assertEqual(db.get_last_processed_week(), "Week 4")

            # Verify backup file created
            bak_path = f"{json_path}.bak"
            self.assertTrue(os.path.exists(bak_path))

            db.close()

    def test_database_manager_run_history(self):
        from src.database import DatabaseManager

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "history.db")
            db = DatabaseManager(db_path=db_path, legacy_json_path=None)

            run_id = db.record_run_start(week_title="Week 5")
            self.assertIsInstance(run_id, int)

            db.record_run_completion(
                run_id=run_id,
                status="SUCCESS",
                week_title="Week 5",
                torrents_added_count=2,
                next_run_at="2026-10-19T06:00:00",
            )

            runs = db.get_recent_runs(limit=5)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["id"], run_id)
            self.assertEqual(runs[0]["status"], "SUCCESS")
            self.assertEqual(runs[0]["torrents_added_count"], 2)
            self.assertEqual(runs[0]["next_run_at"], "2026-10-19T06:00:00")
            self.assertEqual(db.get_next_run_at(), "2026-10-19T06:00:00")

            db.close()

    def test_calculate_next_run_time(self):
        from datetime import datetime
        from src.main import calculate_next_run_time

        allowed_days = ["Mon", "Tue", "Wed"]

        # Monday 10:00 with 6 hour interval -> Monday 16:00
        t1 = datetime(2026, 10, 12, 10, 0, 0)
        next1 = calculate_next_run_time(allowed_days, 6.0, current_dt=t1)
        self.assertEqual(next1, datetime(2026, 10, 12, 16, 0, 0))

        # Wednesday 20:00 with 6 hour interval -> Thu 02:00 (skip) -> Thu 08:00 (skip) ... -> Mon 02:00
        t2 = datetime(2026, 10, 14, 20, 0, 0)
        next2 = calculate_next_run_time(allowed_days, 6.0, current_dt=t2)
        self.assertEqual(next2.strftime("%A"), "Monday")
        self.assertEqual(next2, datetime(2026, 10, 19, 2, 0, 0))

        # No day restrictions -> next tick directly
        next_any = calculate_next_run_time([], 6.0, current_dt=t2)
        self.assertEqual(next_any, datetime(2026, 10, 15, 2, 0, 0))

    def test_format_relative_time(self):
        from datetime import datetime, timedelta
        from src.main import format_relative_time

        base = datetime(2026, 10, 14, 12, 0, 0)

        # 30 minutes ahead
        t1 = base + timedelta(minutes=30)
        self.assertEqual(format_relative_time(t1, base_dt=base), "in 30 minutes")

        # 4 hours ahead
        t2 = base + timedelta(hours=4)
        self.assertEqual(format_relative_time(t2, base_dt=base), "in 4 hours")

        # 2 days, 3 hours ahead
        t3 = base + timedelta(days=2, hours=3)
        self.assertEqual(format_relative_time(t3, base_dt=base), "in 2 days, 3 hours")


if __name__ == "__main__":
    unittest.main()
