"""Logging setup for the application's own loggers (uvicorn configures its own)."""

import logging

from game_server.config import LogLevel

LOGGER_NAME = "game_server"
HANDLER_NAME = "game_server.stderr"

# `trace` is a uvicorn-only level; the standard library's closest match is DEBUG.
_LEVELS: dict[LogLevel, int] = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
}


def configure_logging(level: LogLevel) -> None:
    """Send `game_server.*` log records to stderr at `level`, in uvicorn's line format.

    Safe to call more than once: our handler is added only if it is not already attached,
    regardless of any other handlers (e.g. test capture handlers) on the logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_LEVELS[level])
    logger.propagate = False
    if not any(handler.name == HANDLER_NAME for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name(HANDLER_NAME)
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        logger.addHandler(handler)
