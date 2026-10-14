from typing import Annotated, Optional

from fastapi import Cookie, Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database_config import get_async_session
from src.data.model.user import User
from src.service.auth_service import AuthService, build_auth_service

oauth2_schema = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


async def get_auth_service(db: AsyncSession = Depends(get_async_session)) -> AuthService:
    return build_auth_service(db)


async def get_current_user(
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    access_token: Annotated[Optional[str], Cookie()] = None,
) -> User:
    """
    Utente autenticato della richiesta.

    Firma, scadenza, lista di revoca e revoca globale li verifica
    `AuthService.resolve_current_user`: qui non c'è logica.

    :raises InvalidSession: `401`, sessione assente o non più valida.
    """
    return await auth_service.resolve_current_user(access_token)
