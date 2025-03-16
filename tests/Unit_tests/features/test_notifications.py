import asyncio
import sys
from unittest import TestCase, mock

from aiohttp import ClientSession

from src.features.notifications import NotificationManager


class TestNotificationManager(TestCase):
    def setUp(self) -> None:
        # Create a new event loop for each test
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.notification_manager = NotificationManager(
            notification_url="https://ntfy.sh/test_channel"
        )

        # Set up logging capture
        self.log_messages = []
        self.logger_mock = self.setup_logger_mock()

    def tearDown(self) -> None:
        self.loop.close()
        asyncio.set_event_loop(None)

    def setup_logger_mock(self) -> mock.MagicMock | mock.AsyncMock:
        # Setup mock for logger
        logger_patcher = mock.patch("src.features.notifications.logger")
        mock_logger = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        # Record log messages
        def log_side_effect(message: str) -> None:
            self.log_messages.append(message)

        mock_logger.warning.side_effect = log_side_effect
        mock_logger.error.side_effect = log_side_effect
        mock_logger.info.side_effect = log_side_effect
        return mock_logger

    def run(self, result=None) -> None:  # noqa: ANN001
        """Override run method to handle async test methods"""
        method = getattr(self, self._testMethodName)
        if asyncio.iscoroutinefunction(method):
            self._run_async_test(method, result)
        else:
            super().run(result)

    def _run_async_test(self, method, result):  # noqa: ANN001, ANN202
        """Run an async test method"""
        if result is None:
            result = self.defaultTestResult()
        result.startTest(self)
        try:
            self.setUp()
            try:
                self.loop.run_until_complete(method())
            except Exception:
                result.addError(self, sys.exc_info())
            else:
                result.addSuccess(self)
        finally:
            try:
                self.tearDown()
            except Exception:
                result.addError(self, sys.exc_info())
        return result

    async def test_send_alert_success(self) -> None:
        # Create mock for ClientSession
        mock_session = mock.MagicMock(spec=ClientSession)
        mock_response = mock.MagicMock()
        mock_response.status = 200

        # Make post method return a future that resolves to mock_response
        async def mock_post(*args: tuple, **kwargs: dict) -> mock.MagicMock:
            return mock_response

        mock_session.post = mock_post

        # Test sending a notification
        await self.notification_manager.send_alert(
            mock_session, "Test notification message"
        )

        # Verify notification was sent
        self.assertEqual(self.notification_manager.sent_count, 1)
        self.assertIn("Sent notification: Test notification message", self.log_messages)

    async def test_send_alert_rate_limit(self) -> None:
        # Set up the rate limit to be exceeded
        self.notification_manager.sent_count = self.notification_manager.rate_limit

        mock_session = mock.MagicMock(spec=ClientSession)

        # Create mock post method
        async def mock_post(*args: tuple, **kwargs: dict) -> mock.MagicMock:
            self.fail("Post should not be called when rate limited")

        mock_session.post = mock_post

        # Test sending a notification when rate limit is exceeded
        await self.notification_manager.send_alert(
            mock_session, "This should be rate limited"
        )

        # Check that appropriate warning was logged
        self.assertIn("Rate limit exceeded for notifications", self.log_messages)

    async def test_send_alert_error(self) -> None:
        mock_session = mock.MagicMock(spec=ClientSession)

        # Create mock post method that raises an exception
        async def mock_post(*args: tuple, **kwargs: dict) -> mock.MagicMock:
            raise Exception("Network error")

        mock_session.post = mock_post

        # Test sending a notification with an error
        await self.notification_manager.send_alert(
            mock_session, "This should trigger an error"
        )

        # Check that error was logged
        self.assertIn("Notification failed: Network error", self.log_messages)

        # The sent count should not increase on error
        self.assertEqual(self.notification_manager.sent_count, 0)

    async def test_send_alert_custom_url(self) -> None:
        # Create notification manager with custom URL
        custom_manager = NotificationManager(
            notification_url="https://custom.notification/endpoint"
        )

        mock_session = mock.MagicMock(spec=ClientSession)
        mock_response = mock.MagicMock()
        mock_response.status = 200

        # Track calls to post
        post_calls = []

        # Create mock post method
        async def mock_post(*args: tuple, **kwargs: dict) -> mock.MagicMock:
            post_calls.append((args, kwargs))
            return mock_response

        mock_session.post = mock_post

        # Test sending a notification
        await custom_manager.send_alert(mock_session, "Custom notification")

        # Verify that post was called with the custom URL
        self.assertEqual(len(post_calls), 1)
        args, kwargs = post_calls[0]
        self.assertEqual(args[0], "https://custom.notification/endpoint")
        self.assertEqual(kwargs["data"], b"Custom notification")
        self.assertEqual(kwargs["headers"], {"Title": "Price Alert", "Tags": "warning"})

        # Check that the sent count was incremented
        self.assertEqual(custom_manager.sent_count, 1)
