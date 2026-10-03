"""Errors whose JSON body carries a stable machine-readable `code` beside `detail`.

FastAPI's `HTTPException` only has `detail`; clients branch on `code` instead of the text.
"""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An error response: `{"detail": ..., "code": ...}` with the given status and headers."""

    def __init__(
        self, status_code: int, detail: str, code: str, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.headers = dict(headers or {})


async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
    """Render an `ApiError`."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )


def install(app: FastAPI) -> None:
    """Register the `ApiError` handler on an app."""
    app.add_exception_handler(ApiError, api_error)  # type: ignore[arg-type]  # Starlette types handlers on the base Exception
