import json
import logging
import os
import tempfile
from unittest import TestCase, mock

import pytest

from src.utils.logging_config import CustomJsonFormatter, get_logger, setup_logging


class TestCustomJsonFormatter(TestCase):
    def test_format_basic_record(self) -> None:
        formatter = CustomJsonFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test_path",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        log_data = json.loads(formatted)

        self.assertEqual(log_data["level"], "INFO")
        self.assertEqual(log_data["logger"], "test_logger")
        self.assertEqual(log_data["message"], "Test message")
        self.assertEqual(log_data["module"], "test_path")
        self.assertEqual(log_data["function"], None)
        self.assertEqual(log_data["line"], 42)
        self.assertIn("timestamp", log_data)

    def test_format_with_exception(self) -> None:
        formatter = CustomJsonFormatter()
        try:
            raise ValueError("Test exception")
        except ValueError as e:
            record = logging.LogRecord(
                name="test_logger",
                level=logging.ERROR,
                pathname="test_path",
                lineno=42,
                msg="Exception occurred",
                args=(),
                exc_info=(type(e), e, None),
            )

            formatted = formatter.format(record)
            log_data = json.loads(formatted)

            self.assertEqual(log_data["level"], "ERROR")
            self.assertEqual(log_data["message"], "Exception occurred")
            self.assertIn("exception", log_data)
            self.assertIn("ValueError: Test exception", log_data["exception"])

    def test_format_with_extra_context(self) -> None:
        formatter = CustomJsonFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test_path",
            lineno=42,
            msg="Test message with context",
            args=(),
            exc_info=None,
        )

        # Add extra context
        record.extra = {"url": "https://example.com", "product_id": 123}

        formatted = formatter.format(record)
        log_data = json.loads(formatted)

        self.assertEqual(log_data["level"], "INFO")
        self.assertEqual(log_data["message"], "Test message with context")
        self.assertEqual(log_data["url"], "https://example.com")
        self.assertEqual(log_data["product_id"], 123)


class TestSetupLogging:
    @pytest.fixture
    def temp_log_dir(self) -> str:
        tmpdirname = tempfile.TemporaryDirectory()
        return tmpdirname.name

    def test_setup_logging_creates_directory(self, temp_log_dir: str) -> None:
        log_dir = os.path.join(temp_log_dir, "new_logs_dir")

        # Ensure directory doesn't exist yet
        assert not os.path.exists(log_dir)

        # Setup logging should create the directory
        setup_logging(log_dir=log_dir)

        # Check directory was created
        assert os.path.exists(log_dir)

    def test_setup_logging_configures_handlers(self, temp_log_dir: str) -> None:
        # Setup logging with a test directory
        root_logger = setup_logging(log_dir=temp_log_dir)

        # Verify the root logger level
        assert root_logger.level == logging.DEBUG

        # Count handlers by type
        handler_types = [type(h) for h in root_logger.handlers]

        # Should have one StreamHandler and two file handlers
        assert logging.StreamHandler in handler_types
        assert logging.handlers.TimedRotatingFileHandler in handler_types # type: ignore
        assert logging.handlers.RotatingFileHandler in handler_types # type: ignore

        # Check file paths were created
        assert os.path.exists(os.path.join(temp_log_dir, "price_checker.log"))
        assert os.path.exists(os.path.join(temp_log_dir, "error.log"))

        # Cleanup handlers to avoid affecting other tests
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)


class TestGetLogger(TestCase):
    def test_get_logger_returns_logger(self) -> None:
        logger = get_logger("test_module")

        self.assertIsInstance(logger, logging.Logger)
        self.assertEqual(logger.name, "test_module")

    def test_logger_has_with_context_method(self) -> None:
        logger = get_logger("test_module")

        self.assertTrue(hasattr(logger, "with_context"))
        self.assertTrue(callable(logger.with_context))

    def test_with_context_returns_adapter(self) -> None:
        logger = get_logger("test_module")
        adapter = logger.with_context(product="test_product", url="https://example.com")

        self.assertIsInstance(adapter, logging.LoggerAdapter)
        self.assertEqual(
            adapter.extra,
            {"extra": {"product": "test_product", "url": "https://example.com"}},
        )

    @mock.patch("logging.LoggerAdapter.info")
    def test_adapter_adds_context_to_log(self, mock_info) -> None:  # noqa: ANN001
        logger = get_logger("test_module")
        adapter = logger.with_context(product="test_product", url="https://example.com")

        adapter.info("Test message")

        # Check that info was called with the right message
        mock_info.assert_called_once_with("Test message")
