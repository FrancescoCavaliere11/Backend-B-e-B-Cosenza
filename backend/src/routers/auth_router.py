from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

from backend.src.data.repository.user_repository import UserRepository
from backend.src.data.schemas.auth_schema import AuthResponseSchema
from backend.src.service.auth_service import AuthService
from backend.src.config.database_config import get_async_session


auth_router = APIRouter(prefix="/auth", tags=["Auth"])

async def get_auth_service(db: AsyncSession = Depends(get_async_session)):
    repo = UserRepository(db)
    return AuthService(repo)


@auth_router.post("/token", response_model=AuthResponseSchema)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: Annotated[AuthService, Depends(get_auth_service)]
) -> AuthResponseSchema:
    return await auth_service.authenticate_user(form_data.username, form_data.password)
