"""FastAPI application: HTTP concerns only. All conversion logic is in converters.py."""

import logging
import re
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
)
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware
from slowapi.util import get_remote_address

from src.config import Settings
from src.converters import (
    ConversionError,
    Format,
    Issue,
    MalformedInputError,
    check,
    convert,
)
from src.logging_config import configure_logging
from src.middleware import BodySizeLimitMiddleware, RequestLogMiddleware

logger = logging.getLogger("data_converter.api")

API_KEY_HEADER = "X-API-Key"
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MEDIA_TYPES = {
    Format.CSV: "text/csv; charset=utf-8",
    Format.JSON: "application/json",
    Format.XML: "application/xml",
}


class IssueOut(BaseModel):
    row: int | None
    message: str


class ValidationReport(BaseModel):
    valid: bool
    format: Format
    rows: int
    errors: list[IssueOut]


def _issues_json(issues: list[Issue]) -> list[dict]:
    return [{"row": issue.row, "message": issue.message} for issue in issues]


def _download_name(filename: str | None, target: Format) -> str:
    """Build a header-safe name; the client's filename is untrusted."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename or "").stem).strip("._")
    return f"{stem or 'converted'}.{target.value}"


def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    data = upload.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {max_bytes} bytes.")
    return data


def _infer_format(filename: str | None) -> Format:
    try:
        return Format(Path(filename or "").suffix.lstrip(".").lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Cannot tell the format from the filename; pass ?format=csv|json|xml.",
        ) from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging()

    def is_valid_key(key: str | None) -> bool:
        return key is not None and secrets.compare_digest(key.encode(), settings.api_key.encode())

    def rate_limit_key(request: Request) -> str:
        # Behind a load balancer client addresses can all look alike, so authenticated
        # callers get their own bucket. Invalid keys share the address bucket, which stops
        # anyone escaping the limit by sending a new random key each time.
        key = request.headers.get(API_KEY_HEADER)
        return f"key:{key}" if is_valid_key(key) else get_remote_address(request)

    limiter = Limiter(key_func=rate_limit_key, default_limits=[settings.rate_limit])

    def require_api_key(
        key: Annotated[str | None, Security(APIKeyHeader(name=API_KEY_HEADER, auto_error=False))],
    ) -> None:
        if not is_valid_key(key):
            raise HTTPException(status_code=401, detail="Missing or invalid API key.")

    app = FastAPI(
        title="Data Converter API",
        description="Convert CSV, JSON and XML files, and list validation errors by row.",
        version="0.1.0",
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # The last one added runs first.
    app.add_middleware(SlowAPIASGIMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
    )
    app.add_middleware(RequestLogMiddleware)

    @app.exception_handler(ConversionError)
    def handle_conversion_error(_: Request, exc: ConversionError) -> JSONResponse:
        status = 400 if isinstance(exc, MalformedInputError) else 422
        return JSONResponse(
            status_code=status, content={"detail": str(exc), "errors": _issues_json(exc.errors)}
        )

    @app.get("/health")
    @limiter.exempt
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # These are sync on purpose: FastAPI runs them in a thread pool, so a slow conversion
    # cannot block the event loop and with it the /health probe.
    @app.post(
        "/convert",
        dependencies=[Depends(require_api_key)],
        response_class=Response,
        responses={
            200: {"description": "The converted file.", "content": {"text/plain": {}}},
            400: {"description": "Empty, binary or syntactically invalid file."},
            401: {"description": "Missing or invalid API key."},
            413: {"description": "File too large."},
            422: {"description": "Invalid rows, or invalid request parameters."},
            429: {"description": "Rate limit exceeded."},
        },
    )
    def convert_file(
        source: Annotated[Format, Query(alias="from", description="Format of the upload.")],
        target: Annotated[Format, Query(alias="to", description="Format to return.")],
        file: Annotated[UploadFile, File(description="The file to convert.")],
    ) -> Response:
        data = _read_upload(file, settings.max_upload_bytes)
        output = convert(data, source, target)
        logger.info(
            "converted",
            extra={"fields": {"from": source, "to": target, "size_bytes": len(data)}},
        )
        filename = _download_name(file.filename, target)
        return Response(
            # A JSON file can hold a lone surrogate, which UTF-8 cannot encode.
            content=output.encode("utf-8", "replace"),
            media_type=MEDIA_TYPES[target],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post(
        "/validate",
        dependencies=[Depends(require_api_key)],
        responses={
            400: {"description": "Format not given and not inferable from the filename."},
            401: {"description": "Missing or invalid API key."},
            413: {"description": "File too large."},
            429: {"description": "Rate limit exceeded."},
        },
    )
    def validate_file(
        file: Annotated[UploadFile, File(description="The file to check.")],
        format: Annotated[
            Format | None, Query(description="Defaults to the file's extension.")
        ] = None,
    ) -> ValidationReport:
        """Report every problem in a file. An invalid file is still a 200: the report is
        the successful result. `row` is the line in the file, except for items of a JSON
        array, where it is the item's 1-based position."""
        source = format or _infer_format(file.filename)
        rows, issues = check(_read_upload(file, settings.max_upload_bytes), source)
        return ValidationReport(
            valid=not issues, format=source, rows=rows, errors=_issues_json(issues)
        )

    return app
