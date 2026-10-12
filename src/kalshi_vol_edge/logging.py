"""structlog configuration shared by the CLI and library entry points."""

from __future__ import annotations

import logging
import sys

import structlog


def _stderr_logger(*_: object) -> structlog.PrintLogger:
    return structlog.PrintLogger(sys.stderr)


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure structlog once. Secrets are never passed to the logger by this package."""
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper()) if isinstance(level, str) else level
        ),
        # Resolve sys.stderr per logger, not once: it may be swapped (pytest capture, daemons).
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a bound logger."""
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
