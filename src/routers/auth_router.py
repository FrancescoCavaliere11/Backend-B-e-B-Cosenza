import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from src.config.config import settings
from src.exception.custom_exception import InvalidCredentials
from src.security.authentication import get_auth_service
from src.security.masking import mask_email
from src.security.rate_limiter import (
    clear_login_failures,
    client_ip,
    enforce_login_attempt_limits,
    login_rate_limit,
    record_login_failure,
    refresh_rate_limit,
)
from src.service.auth_service import AuthResult, AuthService

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"

#: Il refresh token viaggia **solo** verso le rotte di autenticazione, non con
#: ogni richiesta al backend: è la credenziale più preziosa (dura 30 giorni) e
#: va esposta il meno possibile.
REFRESH_COOKIE_PATH = auth_router.prefix

#: Attributi comuni. Cancellare un cookie richiede **gli stessi** attributi con
#: cui è stato impostato, altrimenti il browser lo tiene: per questo esistono in
#: un punto solo.
_COOKIE_ATTRIBUTES = dict(
    httponly=True,
    samesite="lax",
    secure=False,  # todo Mettere True in produzione con HTTPS (debito #20)
)


def _set_session_cookies(response: Response, result: AuthResult) -> None:
    access_seconds = settings.access_token_expire_minutes * 60
    refresh_seconds = settings.refresh_token_expire_minutes * 60
    response.set_cookie(
        ACCESS_COOKIE, result.access_token,
        max_age=access_seconds, expires=access_seconds, path="/", **_COOKIE_ATTRIBUTES,
    )
    response.set_cookie(
        REFRESH_COOKIE, result.refresh_token,
        max_age=refresh_seconds, expires=refresh_seconds, path=REFRESH_COOKIE_PATH, **_COOKIE_ATTRIBUTES,
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", **_COOKIE_ATTRIBUTES)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, **_COOKIE_ATTRIBUTES)


@auth_router.post("/token", dependencies=[Depends(login_rate_limit)])
async def login(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
):
    """
    Login con email e password; i token viaggiano in cookie `httponly`.

    Difese contro i tentativi ripetuti (debito #19), nell'ordine:

    1. limite per IP su tutti i tentativi (`login_rate_limit`);
    2. blocco per account e per account + IP sui **fallimenti** recenti,
       verificato **prima** della password, quindi senza costo di CPU;
    3. a esito noto: fallimento contato e registrato nei log, oppure contatore
       `(account, IP)` azzerato.

    Nei log l'email è sempre mascherata; la password non compare mai.
    """
    email = form_data.username
    ip = client_ip(request)

    await enforce_login_attempt_limits(email, ip)

    try:
        result = await auth_service.authenticate_user(email, form_data.password)
    except InvalidCredentials:
        await record_login_failure(email, ip)
        logger.warning("Login fallito: utente=%s ip=%s", mask_email(email), ip)
        raise

    await clear_login_failures(email, ip)
    logger.info("Login riuscito: utente_id=%s ip=%s", result.user_id, ip)

    _set_session_cookies(response, result)
    return {"message": "Login effettuato con successo"}


@auth_router.post("/refresh", dependencies=[Depends(refresh_rate_limit)])
async def refresh(
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    refresh_token: Annotated[Optional[str], Cookie()] = None,
    access_token: Annotated[Optional[str], Cookie()] = None,
):
    """
    Rinnova la sessione: coppia nuova, la vecchia revocata (rotazione, debito #30).

    `401` se il refresh token manca, è scaduto, revocato o già usato; nel caso di
    un riuso sospetto vengono revocate tutte le sessioni dell'utente.
    """
    result = await auth_service.refresh(refresh_token, access_token)
    _set_session_cookies(response, result)
    return {"message": "Sessione rinnovata"}


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    refresh_token: Annotated[Optional[str], Cookie()] = None,
    access_token: Annotated[Optional[str], Cookie()] = None,
) -> None:
    """
    Chiude la sessione: token revocati e cookie cancellati (debito #30).

    Idempotente: `204` anche senza cookie o con token già scaduti o revocati.
    """
    user_id = await auth_service.logout(access_token, refresh_token)
    _clear_session_cookies(response)
    if user_id is not None:
        logger.info("Logout: utente_id=%s ip=%s", user_id, client_ip(request))
