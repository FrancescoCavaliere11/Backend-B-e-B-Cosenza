from fastapi import APIRouter, Depends, Response
from fastapi.security import OAuth2PasswordRequestForm
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.repository.user_repository import UserRepository
from src.service.auth_service import AuthService
from src.config.database_config import get_async_session
from src.config.config import settings


auth_router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

async def get_auth_service(db: AsyncSession = Depends(get_async_session)):
    repo = UserRepository(db)
    return AuthService(repo)


@auth_router.post("/token")
async def login(
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: Annotated[AuthService, Depends(get_auth_service)]
):
    access_token, refresh_token = auth_service.authenticate_user(form_data.username, form_data.password)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        expires=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        secure=False,  # todo Mettere TRUE in produzione quando avrò HTTPS!
        path="/"
    )

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=settings.refresh_token_expire_minutes * 60,
        expires=settings.refresh_token_expire_minutes * 60,
        samesite="lax",
        secure=False,  # todo Mettere TRUE in produzione!
        path="/"
    )

    return {"message": "Login effettuato con successo"}

