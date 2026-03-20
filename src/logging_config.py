"""
Centralized logging configuration using loguru.
================================================
Call ``setup_logging(verbosity)`` once at startup.  All stdlib ``logging``
calls (from uvicorn, httpx, edgartools, FastAPI, etc.) are intercepted and
routed through loguru so every log line shares the same format and level.

Verbosity levels
----------------
  1 → ERROR
  2 → WARNING
  3 → INFO   (default)
  4 → DEBUG
"""
from __future__ import annotations

import logging
import sys

from loguru import logger

_VERBOSITY_LEVEL = {
    1: "ERROR",
    2: "WARNING",
    3: "INFO",
    4: "DEBUG",
}


class _InterceptHandler(logging.Handler):
    """Bridge stdlib ``logging`` → loguru, preserving the original caller."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)

        # Walk up the call stack to find the real caller (skip logging internals)
        frame, depth = sys._getframe(6), 6  # pylint: disable=protected-access
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(verbosity: int = 3) -> None:
    """Configure loguru as the sole logging sink for the whole application.

    Args:
        verbosity: 1=ERROR, 2=WARNING, 3=INFO (default), 4=DEBUG
    """
    level = _VERBOSITY_LEVEL.get(verbosity, "INFO")

    # Replace loguru's default handler with our configured one
    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=(verbosity >= 4),
    )

    # Intercept all stdlib logging so uvicorn/httpx/edgartools/FastAPI go through loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in list(logging.root.manager.loggerDict):  # pylint: disable=no-member
        lib_logger = logging.getLogger(name)
        lib_logger.handlers = [_InterceptHandler()]
        lib_logger.propagate = False

    logger.info("Logging configured: level={} (verbosity={})", level, verbosity)
