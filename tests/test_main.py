from pytest_mock import MockerFixture

from game_server import __main__ as entrypoint
from game_server.config import Settings


def test_main_runs_uvicorn_with_configured_settings(mocker: MockerFixture) -> None:
    mocker.patch.object(
        entrypoint,
        "get_settings",
        return_value=Settings(host="127.0.0.1", port=9300, log_level="debug"),
    )
    run = mocker.patch("game_server.__main__.uvicorn.run")

    entrypoint.main()

    run.assert_called_once_with(
        "game_server.app:app", host="127.0.0.1", port=9300, log_level="debug"
    )
