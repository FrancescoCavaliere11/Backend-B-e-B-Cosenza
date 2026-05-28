from json import JSONDecodeError

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from nbformat.v1.nbjson import JSONReader
from pydantic import ValidationError
from starlette import status
from starlette.responses import JSONResponse

from src.exception.custom_exception import EntityNotFound, EntityAlreadyExists, InvalidFileType, InvalidFileSize


def setup_exception_handler(app: FastAPI):
    # Validation exception handling
    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_exception_handler(request: Request, exc: Exception):
        errors = exc.errors() if hasattr(exc, "errors") else []

        error_message = "I dati inseriti non sono validi."

        if errors:
            first_error = errors[0]
            campo = first_error.get("loc", ["campo"])[-1]
            tipo_errore = first_error.get("msg", "")

            if "string_too_short" in first_error.get("type", "") or "at least" in tipo_errore:
                error_message = f"Il campo '{campo}' è troppo corto."
            elif "string_too_long" in first_error.get("type", "") or "at most" in tipo_errore:
                error_message = f"Il campo '{campo}' supera la lunghezza massima consentita."
            elif "missing" in first_error.get("type", ""):
                error_message = f"Il campo '{campo}' è obbligatorio."
            else:
                error_message = f"Errore sul campo '{campo}': {tipo_errore}"

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "message": error_message,
                "details": errors
            },
        )

    @app.exception_handler(JSONDecodeError)
    async def json_decode_exception_handler(request: Request, exc: JSONDecodeError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"message": "Il formato dei dati inviati non è valido (JSON corrotto)"},
        )



    # Custom exception handling
    @app.exception_handler(EntityNotFound)
    async def entity_not_found_handler(request: Request, exc: EntityNotFound):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": exc.message},
        )

    @app.exception_handler(EntityAlreadyExists)
    async def entity_already_exists_handler(request: Request, exc: EntityAlreadyExists):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"message": exc.message},
        )

    @app.exception_handler(InvalidFileType)
    async def invalid_file_type_handler(request: Request, exc: InvalidFileType):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"message": exc.message},
        )

    @app.exception_handler(InvalidFileSize)
    async def invalid_file_size_handler(request: Request, exc: InvalidFileSize):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"message": exc.message},
        )