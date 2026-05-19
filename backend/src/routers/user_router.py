from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from backend.src.config.database_config import get_async_session
from backend.src.data.schemas.user_schema import UserCreateSchema
from backend.src.data.repository.user_repository import UserRepository
from backend.src.service.user_service import UserService

user_router = APIRouter(prefix="/users", tags=["Users"])

async def get_user_service(db: AsyncSession = Depends(get_async_session)):
    repo = UserRepository(db)
    return UserService(repo)

@user_router.post("/", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateSchema,
    service: Annotated[UserService, Depends(get_user_service)]
) -> None:
    return await service.register_new_user(payload)