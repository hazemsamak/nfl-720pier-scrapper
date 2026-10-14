import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
import requests

logger = logging.getLogger(__name__)

# Discord embed color codes
COLOR_RED = 15158332     # 0xE74C3C - Error/Failure
COLOR_GREEN = 3066993    # 0x2ECC71 - Success
COLOR_ORANGE = 15105570  # 0xE67E22 - Warning


class Notifier:
    """Dispatches notifications to Discord and Telegram."""

    def __init__(
        self,
        discord_webhook_url: Optional[str] = None,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        cooldown_seconds: int = 1800,
    ):
        self.discord_webhook_url = discord_webhook_url
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.cooldown_seconds = cooldown_seconds
        self._recent_errors: Dict[str, float] = {}

    def notify_torrents_added(self, week_title: str, torrents: list) -> None:
        """Notifies user when new torrents have been queued into qBittorrent."""
        if not torrents:
            return

        lines = [f"🏈 **NFL THUUZ Torrent Grabber** - {week_title}"]
        lines.append(f"Successfully added {len(torrents)} torrent(s) to qBittorrent:")
        for idx, t in enumerate(torrents, 1):
            title = t.get("title", "Unknown Title")
            rating = t.get("rating", "N/A")
            fav_badge = " ⭐ *(Favorite Team)*" if t.get("is_favorite_team") else ""
            lines.append(f"{idx}. [{rating}/100]{fav_badge} {title}")

        message = "\n".join(lines)
        self.send_discord(message)
        self.send_telegram(message)

    def notify_error(
        self,
        title: str,
        details: str,
        suggestion: Optional[str] = None,
        context: Optional[Dict[str, str]] = None,
        force: bool = False,
    ) -> bool:
        """
        Dispatches an error alert to Discord and Telegram.
        Deduplicates identical error alerts within cooldown_seconds unless force=True.
        Returns True if alert was dispatched, False if throttled.
        """
        cache_key = f"{title}:{details[:80]}"
        now = time.time()
        last_sent = self._recent_errors.get(cache_key)

        if not force and last_sent and (now - last_sent) < self.cooldown_seconds:
            logger.info("Throttling repeated error notification for '%s' (sent %.0fs ago)", title, now - last_sent)
            return False

        self._recent_errors[cache_key] = now
        self.send_discord_alert(title=title, details=details, suggestion=suggestion, context=context)
        self.send_telegram_alert(title=title, details=details, suggestion=suggestion, context=context)
        return True

    def send_discord_alert(
        self,
        title: str,
        details: str,
        suggestion: Optional[str] = None,
        context: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Sends a rich formatted error embed to the Discord webhook."""
        if not self.discord_webhook_url:
            return False

        embed = {
            "title": f"🚨 {title}",
            "description": details,
            "color": COLOR_RED,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {
                "text": "NFL THUUZ Torrent Grabber",
            },
            "fields": [],
        }

        if context:
            for k, v in context.items():
                if v:
                    embed["fields"].append({
                        "name": str(k),
                        "value": str(v)[:1024],
                        "inline": True,
                    })

        if suggestion:
            embed["fields"].append({
                "name": "💡 Recommended Action",
                "value": suggestion[:1024],
                "inline": False,
            })

        payload = {
            "content": f"⚠️ **NFL Grabber Alert:** {title}",
            "embeds": [embed],
        }

        return self._post_discord_payload(payload)

    def send_discord(self, message: str) -> bool:
        """Sends a plain text message to the Discord webhook."""
        if not self.discord_webhook_url:
            return False
        return self._post_discord_payload({"content": message})

    def _post_discord_payload(self, payload: dict) -> bool:
        try:
            resp = requests.post(self.discord_webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                logger.info("Discord notification sent successfully.")
                return True
            else:
                logger.warning("Discord notification returned status %d: %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Error sending Discord notification: %s", e)
            return False

    def send_telegram_alert(
        self,
        title: str,
        details: str,
        suggestion: Optional[str] = None,
        context: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Sends an error alert formatted for Telegram."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return False

        lines = [f"🚨 *NFL Grabber Alert - {title}*", "", f"*Details:* {details}"]
        if context:
            for k, v in context.items():
                if v:
                    lines.append(f"*{k}:* `{v}`")
        if suggestion:
            lines.append("")
            lines.append(f"💡 *Action:* {suggestion}")

        return self.send_telegram("\n".join(lines))

    def send_telegram(self, message: str) -> bool:
        """Sends a message via Telegram bot."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.telegram_chat_id,
                "text": message.replace("**", "*"),
                "parse_mode": "Markdown",
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("Telegram notification sent successfully.")
                return True
            else:
                logger.warning("Telegram notification returned status %d: %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.error("Error sending Telegram notification: %s", e)
            return False
