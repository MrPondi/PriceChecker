# notifications.py

from aiohttp import ClientSession

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class NotificationManager:
    def __init__(
        self, notification_url: str = "https://ntfy.sh/your_channel"
    ) -> None:
        self.rate_limit = 50  # Notifications per minute
        self.sent_count = 0
        self.notification_url = notification_url

    async def send_alert(self, session: ClientSession, message: str) -> None:
        if self.sent_count >= self.rate_limit:
            logger.warning("Rate limit exceeded for notifications")
            return

        try:
            await session.post(
                self.notification_url,
                data=message.encode("utf-8"),
                headers={"Title": "Price Alert", "Tags": "warning"},
            )
            self.sent_count += 1
            logger.info(f"Sent notification: {message}")
        except Exception as e:
            logger.error(f"Notification failed: {str(e)}")
