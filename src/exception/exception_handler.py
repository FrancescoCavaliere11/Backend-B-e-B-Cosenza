from json import JSONDecodeError
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm.exc import StaleDataError
from starlette import status
from starlette.responses import JSONResponse

from src.exception.custom_exception import AppException, RateLimitExceeded


#: Chiavi rimosse dal dettaglio errori prima di restituirlo al client.
#:
#: - `input` contiene il **valore rifiutato**: su una registrazione con password
#:   debole significherebbe restituire la password in chiaro nella risposta HTTP
#:   (e potenzialmente nei log del frontend o di un proxy).
#: - `url` è un link alla documentazione di Pydantic, inutile per il client e
#:   rivelatore della versione della libreria in uso.
_REDACTED_ERROR_KEYS = frozenset({"input", "url"})

#: Prefisso che Pydantic v2 antepone ai messaggi dei `ValueError` sollevati dai
#: validator custom. Va rimosso: i messaggi del progetto sono già scritti per
#: l'utente finale.
_PYDANTIC_VALUE_ERROR_PREFIX = "Value error, "


def _sanitize_validation_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Rende serializzabile in JSON il dettaglio degli errori di validazione e ne
    rimuove le informazioni sensibili.

    Pydantic v2 inserisce nella chiave `ctx` l'**oggetto eccezione vivo**
    (es. `ValueError(...)`) sollevato dal validator. `json.dumps` non sa
    serializzarlo e solleva `TypeError`, trasformando quello che doveva essere
    un 422 in un 500 e facendo perdere il messaggio d'errore.

    :param errors: lista prodotta da `ValidationError.errors()`.
    :return: nuova lista, sicura da serializzare e da esporre.
    """
    sanitized: List[Dict[str, Any]] = []

    for error in errors:
        clean: Dict[str, Any] = {
            key: value for key, value in error.items() if key not in _REDACTED_ERROR_KEYS
        }

        ctx = error.get("ctx")
        if ctx:
            clean["ctx"] = {key: str(value) for key, value in ctx.items()}

        sanitized.append(clean)

    return sanitized


def _build_error_message(first_error: Dict[str, Any]) -> str:
    """
    Compone il messaggio in italiano mostrato all'utente a partire dal primo
    errore di validazione.

    :param first_error: primo elemento della lista restituita da `errors()`.
    :return: messaggio leggibile.
    """
    loc = first_error.get("loc") or ("campo",)
    campo = loc[-1]

    error_type = first_error.get("type", "")
    tipo_errore = first_error.get("msg", "")

    if tipo_errore.startswith(_PYDANTIC_VALUE_ERROR_PREFIX):
        tipo_errore = tipo_errore[len(_PYDANTIC_VALUE_ERROR_PREFIX):]

    if "string_too_short" in error_type or "at least" in tipo_errore:
        return f"Il campo '{campo}' è troppo corto."

    if "string_too_long" in error_type or "at most" in tipo_errore:
        return f"Il campo '{campo}' supera la lunghezza massima consentita."

    if "missing" in error_type:
        return f"Il campo '{campo}' è obbligatorio."

    if error_type == "value_error":
        # Messaggio prodotto dai validator custom del progetto: è già scritto
        # in italiano e pensato per l'utente finale.
        return tipo_errore

    return f"Errore sul campo '{campo}': {tipo_errore}"


def setup_exception_handler(app: FastAPI):
    # ------------------------------------------------------------------ #
    # Errori di validazione (Pydantic / FastAPI)                          #
    # ------------------------------------------------------------------ #
    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_exception_handler(request: Request, exc: Exception):
        raw_errors = exc.errors() if hasattr(exc, "errors") else []

        error_message = "I dati inseriti non sono validi."
        if raw_errors:
            error_message = _build_error_message(raw_errors[0])

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "message": error_message,
                "details": _sanitize_validation_errors(raw_errors)
            },
        )

    @app.exception_handler(JSONDecodeError)
    async def json_decode_exception_handler(request: Request, exc: JSONDecodeError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"message": "Il formato dei dati inviati non è valido (JSON corrotto)"},
        )

    # ------------------------------------------------------------------ #
    # Eccezioni di business                                               #
    # ------------------------------------------------------------------ #
    # Starlette risolve l'handler percorrendo l'MRO dell'eccezione: registrarlo
    # su `AppException` copre automaticamente tutte le sottoclassi, presenti e
    # future. Lo status code viaggia con l'eccezione stessa.
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.message},
        )

    # Handler più specifico per il rate limiting: Starlette risolve percorrendo
    # l'MRO dell'eccezione, quindi questo prevale su quello generico e aggiunge
    # l'header `Retry-After`, che dice al client quando ha senso riprovare.
    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.message},
            headers=headers,
        )

    # ------------------------------------------------------------------ #
    # Conflitti di concorrenza (optimistic locking)                       #
    # ------------------------------------------------------------------ #
    # SQLAlchemy solleva StaleDataError quando l'UPDATE di un'entità con
    # `version_id_col` non trova la riga alla versione attesa: significa che
    # qualcun altro l'ha modificata nel frattempo. È un conflitto di business,
    # non un errore interno.
    @app.exception_handler(StaleDataError)
    async def stale_data_exception_handler(request: Request, exc: StaleDataError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "message": "Il dato è stato modificato da un'altra operazione nel frattempo. "
                           "Ricarica la pagina e riprova."
            },
        )
