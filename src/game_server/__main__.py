"""Entry point: `python -m game_server` or the `game-server` console script."""

import uvicorn

from game_server.config import get_settings


def main() -> None:
    """Start the HTTP server using the configured host, port and log level."""
    settings = get_settings()
    uvicorn.run(
        "game_server.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
