"""JSON structlog config. Every log line carries invoice_id and attempt_index when in scope."""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # No explicit `file=` -- PrintLoggerFactory's default binds to structlog's own stdout
        # sentinel, which PrintLogger re-resolves against the live sys.stdout on every write
        # instead of snapshotting whatever sys.stdout happened to be at configure_logging()
        # time. Passing file=sys.stdout here previously snapshotted pytest's capsys-redirected
        # stream during test_logging.py, which capsys then closes after that test -- poisoning
        # every subsequent logger.* call in the same pytest session ("I/O operation on closed
        # file"), since structlog.configure() is global process state.
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


get_logger = structlog.get_logger
