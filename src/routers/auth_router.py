import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.config import settings
from src.config.database_config import get_async_session
from src.data.repository.user_repository import UserRepository
from src.exception.custom_exception import InvalidCredentials
from src.security.masking import mask_email
from src.security.rate_limiter import (
    clear_login_failures,
    client_ip,
    enforce_login_attempt_limits,
    login_rate_limit,
    record_login_failure,
)
from src.service.auth_service import AuthService

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


async def get_auth_service(db: AsyncSession = Depends(get_async_session)) -> AuthService:
    return AuthService(UserRepository(db))


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

    response.set_cookie(
        key="access_token",
        value=result.access_token,
        httponly=True,
        max_age=settings.access_token_expire_minutes * 60,
        expires=settings.access_token_expire_minutes * 60,
        samesite="lax",
        secure=False,  # todo Mettere TRUE in produzione quando avrò HTTPS! (debito #20)
        path="/"
    )

    response.set_cookie(
        key="refresh_token",
        value=result.refresh_token,
        httponly=True,
        max_age=settings.refresh_token_expire_minutes * 60,
        expires=settings.refresh_token_expire_minutes * 60,
        samesite="lax",
        secure=False,  # todo Mettere TRUE in produzione! (debito #20)
        path="/"
    )

    return {"message": "Login effettuato con successo"}
