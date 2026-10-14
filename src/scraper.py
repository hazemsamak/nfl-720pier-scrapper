import logging
import os
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests
except ImportError:
    import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://720pier.ru"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://720pier.ru/",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


class PierScraper:
    """Scrapes 720pier.ru for the latest NFL THUUZ rated matches and downloads .torrent files."""

    def __init__(
        self,
        thread_url: str = "https://720pier.ru/viewtopic.php?t=76272",
        username: Optional[str] = None,
        password: Optional[str] = None,
        cookie_string: Optional[str] = None,
        quality_preference: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ):
        self.thread_url = thread_url
        self.username = username
        self.password = password
        self.cookie_string = cookie_string
        self.quality_preference = quality_preference.lower() if quality_preference else None
        self.proxy_url = proxy_url

        self.session = requests.Session()
        if self.proxy_url:
            logger.info("Configuring proxy: %s", self.proxy_url)
            self.session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
        self.session.headers.update(DEFAULT_HEADERS)
        self._setup_auth()

    def _setup_auth(self) -> None:
        """Configures session cookies and/or attempts phpBB login."""
        # 1. Apply session cookies if provided
        if self.cookie_string:
            logger.info("Setting session cookies from provided PIER_COOKIE string.")
            self.session.headers["cookie"] = self.cookie_string
            for cookie in self.cookie_string.split(";"):
                cookie = cookie.strip()
                if "=" in cookie:
                    key, val = cookie.split("=", 1)
                    try:
                        self.session.cookies.set(key.strip(), val.strip(), domain="720pier.ru")
                    except Exception:
                        pass

        # 2. Attempt login if credentials are provided and cookies aren't already set
        if self.username and self.password and not self.cookie_string:
            self._login()

    def _login(self) -> bool:
        """Attempts standard phpBB login via /ucp.php?mode=login."""
        login_url = f"{BASE_URL}/ucp.php?mode=login"
        logger.info("Attempting login to 720pier.ru as '%s'...", self.username)
        try:
            # First GET the login page to acquire any CSRF tokens / session SID
            res = self.session.get(login_url, timeout=15)
            soup = BeautifulSoup(res.text, "lxml")

            # Extract any hidden inputs (e.g. form_token, sid)
            data = {
                "username": self.username,
                "password": self.password,
                "login": "Вход",
                "redirect": "index.php",
            }
            for hidden in soup.find_all("input", type="hidden"):
                name = hidden.get("name")
                val = hidden.get("value", "")
                if name and name not in data:
                    data[name] = val

            post_resp = self.session.post(login_url, data=data, timeout=15)
            if "mode=logout" in post_resp.text or "Выход" in post_resp.text:
                logger.info("Successfully logged in to 720pier.ru.")
                return True
            else:
                logger.warning(
                    "Login response did not confirm logout link. "
                    "If CAPTCHA is enabled, set PIER_COOKIE in .env."
                )
                return False
        except Exception as e:
            logger.error("Failed to login to 720pier: %s", e)
            return False

    def fetch_thread_html(self, url: Optional[str] = None) -> str:
        target_url = url or self.thread_url
        logger.info("Fetching thread HTML from %s...", target_url)
        resp = self.session.get(target_url, timeout=20)
        resp.raise_for_status()
        return resp.text

    def find_latest_week_section(self, soup: BeautifulSoup) -> Tuple[str, BeautifulSoup]:
        """
        Locates the latest week in the forum thread.
        Weeks can be represented in phpBB spoiler blocks, header titles, or post contents.
        Returns: (week_title, section_soup_element)
        """
        # Find all candidates for week blocks
        # 1. Check spoiler blocks (including HTML5 <details class="spoiler">)
        candidates = []
        spoilers = soup.find_all(["details", "div", "section"], class_=lambda c: c and "spoiler" in str(c).lower())
        for sp in spoilers:
            summary = sp.find("summary")
            summary_text = summary.get_text().strip() if summary else ""
            sp_text = summary_text or sp.get_text()
            title_match = re.search(r"(?:week|неделя)\s*0*(\d+)", sp_text, re.IGNORECASE)
            if title_match:
                week_num = int(title_match.group(1))
                candidates.append((week_num, f"Week {week_num}", sp))

        # 2. Check all headings / strong / bold elements for Week N
        if not candidates:
            for elem in soup.find_all(["h1", "h2", "h3", "h4", "strong", "b", "span", "div"]):
                elem_text = elem.get_text().strip()
                title_match = re.match(r"^(?:week|неделя)\s*0*(\d+)\b", elem_text, re.IGNORECASE)
                if title_match:
                    week_num = int(title_match.group(1))
                    parent = elem.find_parent(["details", "div", "td", "li"]) or elem.parent
                    candidates.append((week_num, f"Week {week_num}", parent))

        # 3. Check post bodies directly
        if not candidates:
            posts = soup.find_all("div", class_=lambda c: c and ("content" in c or "postbody" in c))
            for post in posts:
                text = post.get_text()
                matches = list(re.finditer(r"(?:week|неделя)\s*0*(\d+)", text, re.IGNORECASE))
                for m in matches:
                    week_num = int(m.group(1))
                    candidates.append((week_num, f"Week {week_num}", post))

        if not candidates:
            # Fallback to entire first post if no specific week label is parsed
            logger.warning("Could not explicitly match 'Week X' pattern. Fallback to main content container.")
            main_content = soup.find("div", class_="content") or soup.find("div", class_="postbody") or soup
            return ("Latest Week", main_content)

        # Pick candidate with the highest week number
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_num, best_title, best_elem = candidates[0]
        logger.info("Detected latest week: %s (Week #%d)", best_title, best_num)
        return (best_title, best_elem)

    def get_all_matches_in_week(self, week_soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Parses all matches listed inside the week section with their rating and topic/torrent link."""
        matches = []
        thuuz_topic_id = self._extract_topic_id(self.thread_url)

        for link in week_soup.find_all("a", href=True):
            href = link["href"].strip()
            full_url = urljoin(BASE_URL, href)
            link_text = link.get_text().strip() or link.get("title", "").strip()

            if self._is_ignored_link(full_url, thuuz_topic_id):
                continue

            rating = self._extract_rating_near_link(link)
            matches.append({
                "title": link_text,
                "rating": rating,
                "url": full_url,
            })
        return matches

    @staticmethod
    def matches_favorite_team(text: str, favorite_teams: Optional[List[str]]) -> bool:
        """Checks if a match title or link text mentions any configured favorite team."""
        if not favorite_teams or not text:
            return False

        # Build list of search patterns including city and nickname expansions
        patterns = set()
        for team in favorite_teams:
            clean = team.strip()
            if not clean:
                continue
            patterns.add(clean)
            words = clean.split()
            if len(words) >= 2:
                # e.g., "Kansas City Chiefs" -> city "Kansas City", nickname "Chiefs"
                patterns.add(" ".join(words[:-1]))
                patterns.add(words[-1])

        for term in patterns:
            pattern = r"\b" + re.escape(term) + r"\b"
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def extract_top_torrent_links(
        self,
        week_soup: BeautifulSoup,
        limit: int = 2,
        favorite_teams: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """
        Extracts up to `limit` (default: 2) torrent download links from the latest week section,
        plus any match involving `favorite_teams` (e.g. Kansas City Chiefs / KC) even if outside the top limit.
        """
        top_candidates: List[Dict[str, str]] = []
        favorite_candidates: List[Dict[str, str]] = []
        seen_targets = set()

        # Extract THUUZ thread ID to avoid self-referencing
        thuuz_topic_id = self._extract_topic_id(self.thread_url)

        # Find all anchor tags
        links = week_soup.find_all("a", href=True)
        logger.info("Found %d total links inside the latest week section.", len(links))

        for link in links:
            href = link["href"].strip()
            full_url = urljoin(BASE_URL, href)
            link_text = link.get_text().strip() or link.get("title", "").strip()

            # Skip self-links, forum indexes, profile links, etc.
            if self._is_ignored_link(full_url, thuuz_topic_id):
                continue

            if full_url in seen_targets:
                continue

            is_fav = self.matches_favorite_team(link_text, favorite_teams)
            is_top = len(top_candidates) < limit

            # If top quota is met and this is not a favorite team candidate, skip
            if not is_top and not is_fav:
                continue

            seen_targets.add(full_url)
            rating = self._extract_rating_near_link(link)

            torrent_info: Optional[Dict[str, str]] = None

            # Check if this link is a direct torrent download
            if self._is_direct_torrent_link(full_url):
                logger.info("Found direct torrent download link: %s", full_url)
                torrent_info = {
                    "title": link_text or "NFL Game Release",
                    "rating": rating,
                    "torrent_url": full_url,
                    "source": "direct",
                }
            elif "viewtopic.php" in full_url:
                # It's a game release topic thread. Navigate and extract torrent attachment.
                topic_id = self._extract_topic_id(full_url)
                if topic_id == thuuz_topic_id:
                    continue

                logger.info("Resolving game topic release: %s (%s)", link_text, full_url)
                torrent_info = self._extract_torrent_from_game_topic(full_url, link_text)
                if torrent_info:
                    torrent_info["rating"] = rating
                else:
                    # If attachment link could not be parsed, register game entry with topic url
                    logger.info("Using topic URL as fallback torrent candidate for: %s", link_text)
                    torrent_info = {
                        "title": link_text or "NFL Game Release",
                        "rating": rating,
                        "torrent_url": full_url,
                        "topic_url": full_url,
                        "source": "game_topic",
                    }

            if torrent_info:
                torrent_info["matchup"] = link_text
                resolved_title = torrent_info.get("title", "")
                if not is_fav and self.matches_favorite_team(resolved_title, favorite_teams):
                    is_fav = True

                torrent_info["is_favorite_team"] = is_fav

                if is_top:
                    torrent_info["selection_reason"] = "top_and_favorite" if is_fav else "top"
                    top_candidates.append(torrent_info)
                elif is_fav:
                    torrent_info["selection_reason"] = "favorite"
                    favorite_candidates.append(torrent_info)
                    logger.info(
                        "Selected favorite team match outside top %d: [%s pts] %s",
                        limit,
                        rating,
                        resolved_title,
                    )

        combined = top_candidates + favorite_candidates
        logger.info(
            "Extracted %d torrent link(s) for the latest week (%d top-rated, %d additional favorite team match(es)).",
            len(combined),
            len(top_candidates),
            len(favorite_candidates),
        )
        return combined

    def _is_direct_torrent_link(self, url: str) -> bool:
        low = url.lower()
        return (
            ".torrent" in low
            or "download/torrent" in low
            or "download/file.php" in low
            or "mode=download" in low
            or "attachment.php" in low
        )

    def _is_ignored_link(self, url: str, current_topic_id: Optional[str]) -> bool:
        low = url.lower()
        if any(ign in low for ign in ["ucp.php", "mcp.php", "memberlist.php", "faq.php", "index.php", "viewforum.php"]):
            return True
        if current_topic_id and f"t={current_topic_id}" in low:
            return True
        return False

    def _extract_topic_id(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        return qs.get("t", [None])[0]

    def _extract_rating_near_link(self, link_elem) -> str:
        """Extracts Thuuz score (0-100) from the link or its surrounding line/parent text."""
        # 1. Check text immediately preceding the link (previous siblings)
        prev_text = ""
        prev = link_elem.previous_sibling
        while prev:
            if isinstance(prev, str):
                prev_text = prev + prev_text
            elif hasattr(prev, "get_text"):
                prev_text = prev.get_text() + prev_text
            # Stop traversing if we hit a line break or block separator
            if getattr(prev, "name", None) in ["br", "p", "div", "li", "summary"]:
                break
            prev = prev.previous_sibling

        if prev_text:
            match = re.search(r"0*([1-9]?\d|100)\s*(?:[•\-–/]|pts|pt|\))\b", prev_text)
            if match:
                return str(int(match.group(1)))
            match2 = re.search(r"\b0*([1-9]\d|100)\b", prev_text)
            if match2:
                return str(int(match2.group(1)))

        # 2. Check the link's own text or title
        link_text = (link_elem.get_text() + " " + link_elem.get("title", "")).strip()
        match_link = re.search(r"\b0*([1-9]?\d|100)\b", link_text)
        if match_link:
            return str(int(match_link.group(1)))

        # 3. Fallback to parent line containing the link
        parent = link_elem.parent
        if parent:
            for line in parent.get_text().splitlines():
                if link_text and link_text in line:
                    m = re.search(r"\b0*([1-9]?\d|100)\b", line)
                    if m:
                        return str(int(m.group(1)))

        return "N/A"

    def _extract_torrent_from_game_topic(self, topic_url: str, fallback_title: str) -> Optional[Dict[str, str]]:
        """Fetches a game release topic and finds the torrent attachment."""
        try:
            resp = self.session.get(topic_url, headers={"Referer": self.thread_url}, timeout=15)
            if resp.status_code != 200:
                logger.warning("Failed to open game topic %s (status %d)", topic_url, resp.status_code)
                return None

            soup = BeautifulSoup(resp.text, "lxml")
            topic_title = fallback_title
            title_elem = soup.find("h2", class_="topic-title") or soup.find("title")
            if title_elem:
                topic_title = title_elem.get_text().strip()

            torrent_links = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                full_download_url = urljoin(BASE_URL, href)
                if self._is_direct_torrent_link(full_download_url):
                    fn = ""
                    parent_td = a.find_parent("td")
                    if parent_td:
                        prev_td = parent_td.find_previous_sibling("td")
                        if prev_td and ".torrent" in prev_td.get_text():
                            m_fn = re.search(r'([A-Za-z0-9._\-@ ]+\.torrent)', prev_td.get_text())
                            if m_fn:
                                fn = m_fn.group(1).strip()
                    text = (fn + " " + a.get_text() + " " + a.get("title", "")).strip()
                    torrent_links.append((full_download_url, text, fn))

            if not torrent_links:
                logger.warning("No torrent attachment found in game topic: %s", topic_url)
                return None

            # Apply quality preference if specified
            selected_url, selected_text, selected_fn = torrent_links[0]
            if self.quality_preference:
                for url, text, fn in torrent_links:
                    if self.quality_preference in (url + " " + text + " " + topic_title).lower():
                        selected_url, selected_text, selected_fn = url, text, fn
                        logger.info("Selected torrent matching quality preference '%s': %s",
                                    self.quality_preference, selected_url)
                        break

            clean_fn = selected_fn or f"{topic_title}.torrent"
            return {
                "title": topic_title,
                "torrent_url": selected_url,
                "topic_url": topic_url,
                "filename": clean_fn,
                "source": "game_topic",
            }
        except Exception as e:
            logger.error("Error parsing game topic %s: %s", topic_url, e)
            return None

    def download_torrent(
        self, torrent_url: str, default_filename: str = "match.torrent", referer: Optional[str] = None
    ) -> Tuple[bytes, str]:
        """Downloads the .torrent binary payload using the authenticated session."""
        logger.info("Downloading .torrent file from: %s", torrent_url)
        headers = {}
        if referer:
            headers["Referer"] = referer
        elif "download/torrent" in torrent_url:
            headers["Referer"] = self.thread_url
        resp = self.session.get(torrent_url, headers=headers, timeout=20)
        resp.raise_for_status()

        # Determine filename from Content-Disposition header if available
        cd = resp.headers.get("Content-Disposition", "")
        filename = default_filename
        if "filename=" in cd:
            fn_match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
            if fn_match:
                filename = fn_match.group(1).strip()

        if not filename.endswith(".torrent"):
            filename = f"{filename}.torrent"

        # Sanitize filename
        filename = re.sub(r'[\\/*?:"<>|]', "_", filename)
        return resp.content, filename
