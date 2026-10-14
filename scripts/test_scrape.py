import logging
import os
import sys

# Ensure repository root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bs4 import BeautifulSoup
from src.config import Config
from src.scraper import PierScraper

# Configure detailed logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("TestScraper")

def main():
    logger.info("Loading configuration from .env...")
    config = Config.load()
    logger.info("Target URL: %s", config.pier_thread_url)
    logger.info("Username: %s", config.pier_username or "(none)")
    logger.info("Cookie set: %s", "Yes" if config.pier_cookie else "No")
    if config.proxy_url:
        logger.info("Proxy configured: %s", config.proxy_url)

    scraper = PierScraper(
        thread_url=config.pier_thread_url,
        username=config.pier_username,
        password=config.pier_password,
        cookie_string=config.pier_cookie,
        quality_preference=config.quality_preference,
        proxy_url=config.proxy_url,
    )

    try:
        logger.info("Fetching thread HTML...")
        html = scraper.fetch_thread_html()
        logger.info("Fetched %d characters of HTML.", len(html))
    except Exception as e:
        logger.error("Failed to fetch thread HTML: %s", e)
        sys.exit(1)

    soup = BeautifulSoup(html, "lxml")

    # Locate latest week
    week_title, week_elem = scraper.find_latest_week_section(soup)
    print("\n" + "=" * 60)
    print(f"🏈 DETECTED LATEST WEEK: {week_title}")
    print("=" * 60)

    # Extract and display ALL matches in the latest week
    all_matches = scraper.get_all_matches_in_week(week_elem)
    print(f"\n--- All Matches in {week_title} ({len(all_matches)} total) ---")
    for idx, m in enumerate(all_matches, 1):
        print(f"  {idx:2d}. [{m.get('rating'):>3} pts] {m.get('title')} ({m.get('url')})")

    # Extract all links and top torrent candidates
    print(f"\n--- Selected Top 2 Match Candidates for Download ---")
    top_torrents = scraper.extract_top_torrent_links(week_elem, limit=2)
    if not top_torrents:
        print("⚠️ No torrent links found in the latest week section.")
    else:
        for idx, t in enumerate(top_torrents, 1):
            torrent_url = t.get("torrent_url")
            title = t.get("title")
            fn = t.get("filename", f"{title}.torrent")
            print(f"\nCandidate #{idx}:")
            print(f"  Title:   {title}")
            print(f"  Rating:  {t.get('rating')}/100")
            print(f"  URL:     {torrent_url}")
            print(f"  Source:  {t.get('source')}")

            # Test actual torrent file download in memory
            try:
                topic_ref = t.get("topic_url") or scraper.thread_url
                content, real_fn = scraper.download_torrent(torrent_url, fn, referer=topic_ref)
                is_valid = content.startswith(b"d8:announce") or content.startswith(b"d10:created") or b"announce" in content[:100]
                print(f"  📥 Torrent Download Test:")
                print(f"     Filename:  {real_fn}")
                print(f"     Size:      {len(content):,} bytes")
                print(f"     Valid:     {'✅ Valid BitTorrent file' if is_valid else '❌ Unexpected format'}")
            except Exception as dl_err:
                print(f"  ⚠️ Could not download .torrent file: {dl_err}")

    print("\n" + "=" * 60)
    print("Test scrape & download completed successfully (dry run - nothing sent to qBittorrent).")
    print("=" * 60)

if __name__ == "__main__":
    main()
