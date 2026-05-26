from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from src.config.database_config import get_async_session
from src.data.model.user import User
from src.data.schemas.user_schema import UserCreateSchema, CurrentUserSchema
from src.data.repository.user_repository import UserRepository
from src.service.user_service import UserService
from src.security.authentication import get_current_user

user_router = APIRouter(prefix="/api/v1/users", tags=["Users"])

async def get_user_service(db: AsyncSession = Depends(get_async_session)):
    repo = UserRepository(db)
    return UserService(repo)


@user_router.get("/me", response_model=CurrentUserSchema)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@user_router.post("/", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateSchema,
    service: Annotated[UserService, Depends(get_user_service)]
) -> None:
    return await service.register_new_user(payload)