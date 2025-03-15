# logging_config.py
import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast


class CustomJsonFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging"""

    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if hasattr(record, "extra"):
            log_record.update(record.extra)  # type: ignore

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record)


class StructuredLoggerProtocol(logging.Logger):
    """Protocol for a logger with a with_context method"""

    def with_context(self, **context: dict) -> logging.LoggerAdapter: ...


def setup_logging(log_dir: str="logs") -> logging.Logger:
    """Configure application logging with rotation and structured logs"""

    # Create log directory if it doesn't exist
    Path(log_dir).mkdir(exist_ok=True)

    # Console handler for INFO and above
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        "[%(asctime)s %(levelname)s]: %(message)s", datefmt="%x %X"
    )
    console_handler.setFormatter(console_formatter)

    # File handler for DEBUG and above with rotation
    # Keep 7 days of logs, rotate at midnight
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "price_checker.log"),
        when="midnight",
        backupCount=14,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(CustomJsonFormatter())

    # Error file handler for ERROR and above with rotation
    error_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(log_dir, "error.log"),
        maxBytes=10_485_760,  # 10MB
        backupCount=5,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(CustomJsonFormatter())

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(error_handler)

    # Set specific levels for noisy libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger with a `with_context` method for structured logging."""
    logger = logging.getLogger(name)

    # Add the `with_context` method to the logger
    def with_context(**context: dict[str, Any]) -> logging.LoggerAdapter:
        return logging.LoggerAdapter(logger, {"extra": context})

    logger.with_context = with_context  # type: ignore[method-assign]

    return cast(StructuredLoggerProtocol, logger)
