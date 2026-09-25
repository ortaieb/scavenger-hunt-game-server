import logging
import sys
from collections.abc import Iterator

import pytest

from game_server.config import LogLevel
from game_server.logging_config import HANDLER_NAME, LOGGER_NAME, configure_logging


@pytest.fixture
def app_logger() -> Iterator[logging.Logger]:
    """Give each test a clean `game_server` logger and restore it afterwards."""
    logger = logging.getLogger(LOGGER_NAME)
    saved = (logger.level, logger.propagate, list(logger.handlers))
    logger.handlers.clear()
    yield logger
    logger.setLevel(saved[0])
    logger.propagate = saved[1]
    logger.handlers[:] = saved[2]


@pytest.mark.parametrize(
    ("level", "expected"),
    [("info", logging.INFO), ("warning", logging.WARNING), ("trace", logging.DEBUG)],
)
def test_sets_level(app_logger: logging.Logger, level: LogLevel, expected: int) -> None:
    configure_logging(level)

    assert app_logger.level == expected


def own_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [handler for handler in logger.handlers if handler.name == HANDLER_NAME]


def test_adds_single_handler_when_called_twice(app_logger: logging.Logger) -> None:
    configure_logging("info")
    configure_logging("debug")

    assert len(own_handlers(app_logger)) == 1
    assert app_logger.level == logging.DEBUG


def test_adds_handler_even_if_another_is_attached(app_logger: logging.Logger) -> None:
    app_logger.addHandler(logging.NullHandler())

    configure_logging("info")

    assert len(own_handlers(app_logger)) == 1


def test_child_logger_output_reaches_stderr(
    app_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("info")
    # The handler captured sys.stderr when created; point it at pytest's capture.
    [handler] = own_handlers(app_logger)
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(sys.stderr)

    logging.getLogger(f"{LOGGER_NAME}.challenge").info("hello")

    assert capsys.readouterr().err == "INFO:     hello\n"
