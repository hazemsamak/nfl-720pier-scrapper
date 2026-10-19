import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from typing import List, Optional

import schedule
import qbittorrentapi

import os

# Ensure repository root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.config import Config
from src.database import DatabaseManager
from src.notifier import Notifier
from src.qbittorrent import QBittorrentClient
from src.scraper import PierScraper
from src.state import StateManager
from src.web import start_web_server, update_service_status

# Configure clean, informative logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("NFLGrabber")

running = True


def handle_shutdown(signum, frame):
    global running
    logger.info("Received termination signal (%d). Shutting down gracefully...", signum)
    running = False


def format_match_name(week_title: str, torrent_data: dict) -> str:
    """Formats matchup title into 'Week 05 - Team A at Team B (YYYY)'."""
    import re
    from datetime import datetime

    m_week = re.search(r"\d+", week_title or "")
    week_str = f"Week {int(m_week.group(0)):02d}" if m_week else (week_title or "NFL")

    matchup = torrent_data.get("matchup", "").strip()
    if not matchup:
        raw_title = torrent_data.get("title", "")
        m_match = re.search(r"([A-Za-z0-9. ]+?\s+(?:at|@|vs)\s+[A-Za-z0-9. ]+?)(?:\[|\(|$)", raw_title)
        if m_match:
            matchup = m_match.group(1).strip()
        else:
            matchup = raw_title

    matchup = re.sub(r"\s*@\s*", " at ", matchup)

    m_year = re.search(r"\b(20\d\d)\b", torrent_data.get("title", ""))
    year = m_year.group(1) if m_year else str(datetime.now().year)

    return f"{week_str} - {matchup} ({year})"


def is_scheduled_day(allowed_days: Optional[List[str]], dt: Optional[datetime] = None) -> bool:
    """Checks whether the specified datetime (default: now) matches configured allowed schedule days."""
    if not allowed_days:
        return True
    check_dt = dt or datetime.now()
    short_day = check_dt.strftime("%a").lower()
    full_day = check_dt.strftime("%A").lower()
    norm = [d.strip().lower() for d in allowed_days if d.strip()]
    return any(d.startswith(short_day) or short_day.startswith(d) or d == full_day for d in norm)


def calculate_next_run_time(
    allowed_days: Optional[List[str]],
    interval_hours: float,
    current_dt: Optional[datetime] = None,
) -> datetime:
    """
    Calculates the exact next datetime when the grabber will actively run based on schedule days and interval.
    If the next interval step lands on an excluded day (e.g. Thu-Sun), it projects forward
    to the first interval tick on the next allowed day (e.g. Monday).
    """
    from datetime import timedelta

    try:
        hours_val = float(interval_hours)
    except (TypeError, ValueError):
        hours_val = 6.0

    base_dt = current_dt or datetime.now()
    step_delta = timedelta(hours=max(0.1, hours_val))
    candidate = base_dt + step_delta

    # If all days are allowed, simply return the next interval
    if not allowed_days:
        return candidate

    # Advance candidate until it matches an allowed day (up to 30 days maximum lookahead)
    max_steps = int(24 * 30 / max(0.1, hours_val)) + 1
    steps = 0
    while not is_scheduled_day(allowed_days, candidate) and steps < max_steps:
        candidate += step_delta
        steps += 1

    return candidate


def format_relative_time(target_dt: datetime, base_dt: Optional[datetime] = None) -> str:
    """Formats a datetime into a human-readable relative string like 'in 2 hours, 15 minutes'."""
    now = base_dt or datetime.now()
    delta = target_dt - now
    total_seconds = int(delta.total_seconds())

    if total_seconds <= 0:
        return "now"

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days > 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")

    rel = ", ".join(parts) if parts else "less than a minute"
    return f"in {rel}"


def execute_grabber_cycle(
    config: Config, state: StateManager, notifier: Notifier, dry_run: bool = False
) -> int:
    """
    Executes a single grabber pass:
    1. Tests qBittorrent connection & credentials (unless dry_run).
    2. Scrapes latest week and top 2 torrents (plus configured favorite teams).
    3. Filters out already processed items (unless dry_run).
    4. Downloads and pushes new torrents to qBittorrent (unless dry_run).
    5. Dispatches notifications / alerts via Discord and Telegram.
    Returns count of newly added torrents.
    """
    run_id = None
    if hasattr(state, "record_run_start") and not dry_run:
        try:
            run_id = state.record_run_start()
        except Exception as e:
            logger.debug("Run start record notice: %s", e)

    try:
        logger.info("==================================================")
        logger.info("Starting NFL THUUZ torrent check%s...", " (DRY RUN)" if dry_run else "")
        logger.info("Thread URL: %s", config.pier_thread_url)

        scraper = PierScraper(
            thread_url=config.pier_thread_url,
            username=config.pier_username,
            password=config.pier_password,
            cookie_string=config.pier_cookie,
            quality_preference=config.quality_preference,
            proxy_url=config.proxy_url,
        )

        qbit = QBittorrentClient(
            host=config.qbit_host,
            api_key=config.qbit_api_key,
            username=config.qbit_username,
            password=config.qbit_password,
            category=config.qbit_category,
            tags=config.qbit_tags,
            save_path=config.qbit_save_path,
            auto_start=config.qbit_auto_start,
        ) if not dry_run else None

        # 1. Proactively test qBittorrent connectivity & authentication before scraping
        if not dry_run and qbit:
            try:
                client = qbit.get_client()
                if qbit.api_key:
                    _ = client.app.version
                else:
                    if not getattr(getattr(client, "auth", None), "is_logged_in", True):
                        raise qbittorrentapi.LoginFailed("Authentication failed: not logged in to qBittorrent")
            except qbittorrentapi.LoginFailed as e:
                logger.error("qBittorrent authentication failed: %s", e)
                if run_id and hasattr(state, "record_run_completion"):
                    state.record_run_completion(run_id, status="ERROR", error_message="qBittorrent Authentication Failure")
                notifier.notify_error(
                    title="qBittorrent Authentication Failure",
                    details=f"Failed to authenticate with qBittorrent at {config.qbit_host}: {e}",
                    suggestion="Verify QBIT_API_KEY or QBIT_USERNAME/QBIT_PASSWORD in your .env file.",
                    context={"Host": config.qbit_host},
                )
                return 0
            except Exception as e:
                err_str = str(e)
                logger.error("qBittorrent connection failed: %s", e)
                is_auth = any(term in err_str for term in ["401", "403", "Unauthorized", "Forbidden", "Invalid API key"])
                if is_auth:
                    if run_id and hasattr(state, "record_run_completion"):
                        state.record_run_completion(run_id, status="ERROR", error_message="qBittorrent Unauthorized")
                    notifier.notify_error(
                        title="qBittorrent Authentication Failure",
                        details=f"Unauthorized access to qBittorrent at {config.qbit_host}: {e}",
                        suggestion="Verify QBIT_API_KEY in your .env file matches the key generated in qBittorrent WebUI.",
                        context={"Host": config.qbit_host},
                    )
                else:
                    if run_id and hasattr(state, "record_run_completion"):
                        state.record_run_completion(run_id, status="ERROR", error_message="qBittorrent Connection Failure")
                    notifier.notify_error(
                        title="qBittorrent Connection Failure",
                        details=f"Could not connect to qBittorrent server at {config.qbit_host}: {e}",
                        suggestion="Ensure qBittorrent is running, accessible over the network, and the host/port is correct.",
                        context={"Host": config.qbit_host},
                    )
                return 0

        # 2. Fetch 720pier thread HTML
        try:
            html = scraper.fetch_thread_html()
        except Exception as e:
            err_str = str(e)
            logger.error("Failed to fetch THUUZ ratings thread: %s", e)
            if run_id and hasattr(state, "record_run_completion"):
                state.record_run_completion(run_id, status="ERROR", error_message=f"Failed to fetch thread HTML: {e}")
            is_auth = any(term in err_str for term in ["401", "403", "Forbidden", "Unauthorized", "Cloudflare"])
            if is_auth:
                notifier.notify_error(
                    title="720pier Authentication Failure",
                    details=f"Session expired or access forbidden on 720pier: {e}",
                    suggestion="Update PIER_COOKIE in .env with fresh browser session cookies from 720pier.ru.",
                    context={"Thread URL": config.pier_thread_url},
                )
            else:
                notifier.notify_error(
                    title="720pier Connection Failure",
                    details=f"Failed to connect to 720pier forum: {e}",
                    suggestion="Verify network connectivity and check if https://720pier.ru is reachable.",
                    context={"Thread URL": config.pier_thread_url},
                )
            return 0

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        week_title, week_elem = scraper.find_latest_week_section(soup)
        logger.info("Active Week: %s", week_title)
        update_service_status(active_week=week_title)

        top_torrents = scraper.extract_top_torrent_links(
            week_elem,
            limit=2,
            favorite_teams=config.favorite_teams,
        )
        if not top_torrents:
            logger.warning("No torrent links could be extracted from %s.", week_title)
            if run_id and hasattr(state, "record_run_completion"):
                state.record_run_completion(run_id, status="NO_TORRENTS", week_title=week_title)
            return 0

        logger.info("Evaluating %d match candidate(s) for %s...", len(top_torrents), week_title)
        newly_added: List[dict] = []

        for idx, t in enumerate(top_torrents, 1):
            torrent_url = t["torrent_url"]
            title = t.get("title", "NFL Match")
            rating = t.get("rating", "N/A")
            fav_tag = " [FAVORITE TEAM: KC]" if t.get("is_favorite_team") else ""

            if state.is_torrent_processed(torrent_url) and not dry_run:
                logger.info("Skipping already processed torrent: [%s pts]%s %s", rating, fav_tag, title)
                continue

            custom_name = format_match_name(week_title, t) if config.rename_torrent_files else None
            logger.info("Candidate #%d: [%s pts]%s %s (URL: %s)", idx, rating, fav_tag, title, torrent_url)
            if custom_name:
                logger.info("  -> Target formatted file name: %s", custom_name)

            if dry_run:
                logger.info("[DRY RUN] Would download and add to qBittorrent: %s (as '%s')", title, custom_name or title)
                newly_added.append(t)
                continue

            # Download .torrent binary
            try:
                fn = t.get("filename", f"{title}.torrent")
                topic_ref = t.get("topic_url") or scraper.thread_url
                torrent_bytes, filename = scraper.download_torrent(torrent_url, fn, referer=topic_ref)
            except Exception as e:
                err_str = str(e)
                logger.error("Error downloading torrent for '%s': %s", title, e)
                is_auth = any(term in err_str for term in ["401", "403", "Forbidden", "Unauthorized"])
                if is_auth:
                    notifier.notify_error(
                        title="720pier Download Authentication Failure",
                        details=f"Access denied while downloading torrent for '{title}': {e}",
                        suggestion="Your 720pier session cookie may have expired. Refresh PIER_COOKIE in .env.",
                        context={"Match": title, "URL": torrent_url},
                    )
                else:
                    notifier.notify_error(
                        title="Torrent Download Failure",
                        details=f"Failed to download .torrent binary for '{title}': {e}",
                        suggestion="Verify if the forum attachment is still available on 720pier.",
                        context={"Match": title, "URL": torrent_url},
                    )
                continue

            # Forward to qBittorrent
            try:
                success = qbit.add_torrent_file(torrent_bytes, filename, custom_name=custom_name)
                if success:
                    state.record_processed_torrent(torrent_url, title, week_title)
                    newly_added.append(t)
                    logger.info("Successfully added torrent to qBittorrent: %s", filename)
                else:
                    logger.error("Failed to add torrent to qBittorrent: %s", filename)
                    notifier.notify_error(
                        title="qBittorrent Forwarding Failure",
                        details=f"qBittorrent rejected torrent file '{filename}' for match '{title}'.",
                        suggestion="Check qBittorrent disk space, save path permissions (/nfl), and WebUI logs.",
                        context={"Match": title, "File": filename, "Save Path": config.qbit_save_path or "default"},
                    )
            except Exception as e:
                logger.error("Exception while forwarding torrent to qBittorrent for '%s': %s", title, e)
                notifier.notify_error(
                    title="qBittorrent Forwarding Failure",
                    details=f"Exception adding torrent '{filename}' to qBittorrent: {e}",
                    suggestion="Check qBittorrent server connectivity and configuration.",
                    context={"Match": title, "File": filename},
                )

        next_run_dt = calculate_next_run_time(config.schedule_days, config.check_interval_hours)
        next_run_str = f"{next_run_dt.strftime('%A, %b %d at %H:%M')} ({format_relative_time(next_run_dt)})"

        if run_id and hasattr(state, "record_run_completion") and not dry_run:
            try:
                state.record_run_completion(
                    run_id=run_id,
                    status="SUCCESS" if newly_added else "NO_TORRENTS",
                    week_title=week_title,
                    torrents_added_count=len(newly_added),
                    next_run_at=next_run_dt.isoformat(),
                )
            except Exception as e:
                logger.debug("Run completion record notice: %s", e)

        if newly_added and not dry_run:
            logger.info("Summary: %d new torrent(s) pushed to qBittorrent for %s.", len(newly_added), week_title)
            notifier.notify_torrents_added(week_title, newly_added, next_run_str=next_run_str)
        elif dry_run:
            logger.info("[DRY RUN] Finished check. %d match(es) identified for %s.", len(newly_added), week_title)
        else:
            logger.info("No new torrents needed to be added this cycle.")

        logger.info("Cycle finished. Total historical processed torrents: %d", state.get_processed_count())
        logger.info("==================================================")
        return len(newly_added)

    except Exception as e:
        logger.exception("Unexpected error during NFL grabber cycle: %s", e)
        if run_id and hasattr(state, "record_run_completion") and not dry_run:
            try:
                state.record_run_completion(
                    run_id=run_id,
                    status="ERROR",
                    error_message=f"{type(e).__name__}: {e}",
                )
            except Exception:
                pass
        notifier.notify_error(
            title="Unexpected Grabber Error",
            details=f"An unhandled exception halted the grabber cycle: {type(e).__name__}: {e}",
            suggestion="Check application service logs for the complete traceback.",
        )
        return 0


def main():
    parser = argparse.ArgumentParser(description="NFL THUUZ 720pier Torrent Grabber & qBittorrent Forwarder")
    parser.add_argument("--once", action="store_true", help="Run once and exit immediately instead of scheduling")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and show matches without contacting qBittorrent or updating state")
    args = parser.parse_args()

    config = Config.load()

    # Start embedded lightweight web log viewer server
    if getattr(config, "web_enabled", True):
        start_web_server(port=config.web_port, db_path=config.db_path)

    state = DatabaseManager(db_path=config.db_path, legacy_json_path=config.state_file_path)
    notifier = Notifier(
        discord_webhook_url=config.discord_webhook_url,
        telegram_bot_token=config.telegram_bot_token,
        telegram_chat_id=config.telegram_chat_id,
    )

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    if args.dry_run or args.once:
        logger.info("Running in one-shot mode%s.", " (DRY RUN)" if args.dry_run else "")
        execute_grabber_cycle(config, state, notifier, dry_run=args.dry_run)
        sys.exit(0)

    # Scheduled mode
    interval_hours = max(0.1, config.check_interval_hours)
    days_label = ", ".join(config.schedule_days) if config.schedule_days else "Everyday"
    logger.info(
        "Running in scheduled service mode on [%s] every %.2f hour(s). Press Ctrl+C to terminate.",
        days_label,
        interval_hours,
    )

    def log_next_run(base_dt=None):
        next_dt = calculate_next_run_time(config.schedule_days, interval_hours, current_dt=base_dt)
        rel_str = format_relative_time(next_dt, base_dt=base_dt)
        formatted_next = f"{next_dt.strftime('%A, %Y-%m-%d %H:%M:%S')} ({rel_str})"
        update_service_status(next_run=formatted_next, status="Running")
        logger.info(
            "Next grabber cycle scheduled for: %s (%s)",
            next_dt.strftime("%A, %Y-%m-%d %H:%M:%S"),
            rel_str,
        )
        return next_dt

    def run_scheduled_cycle():
        current_day_name = datetime.now().strftime("%A")
        if not is_scheduled_day(config.schedule_days):
            logger.info(
                "Today is %s (not in scheduled days: %s). Skipping check cycle.",
                current_day_name,
                days_label,
            )
            log_next_run()
            return 0
        update_service_status(status="Active Cycle Running")
        res = execute_grabber_cycle(config, state, notifier)
        update_service_status(
            status="Running",
            last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        log_next_run()
        return res

    # Initial run on startup (only if today is an active scheduled day)
    today_name = datetime.now().strftime("%A")
    if is_scheduled_day(config.schedule_days):
        logger.info("Today (%s) is an active scheduled run day. Running initial grabber cycle...", today_name)
        execute_grabber_cycle(config, state, notifier)
    else:
        logger.info(
            "Service started. Today (%s) is outside scheduled active days (%s). Standing by for next interval check.",
            today_name,
            days_label,
        )

    log_next_run()
    schedule.every(interval_hours).hours.do(run_scheduled_cycle)

    while running:
        schedule.run_pending()
        time.sleep(10)

    logger.info("NFL Grabber service terminated.")


if __name__ == "__main__":
    main()
