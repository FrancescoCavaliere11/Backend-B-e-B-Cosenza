from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.security.jwt_handler import decode_and_validate_access_token
from src.data.repository.user_repository import UserRepository
from src.config.database_config import get_async_session

oauth2_schema = OAuth2PasswordBearer(tokenUrl="/auth/token")

async def get_user_repository(db: AsyncSession = Depends(get_async_session)):
    return UserRepository(db)

async def get_current_user(
    token: Annotated[str, Depends(oauth2_schema)],
    user_repository: UserRepository = Depends(get_user_repository),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Impossibile validare le credenziali",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_and_validate_access_token(token)
    user = await user_repository.get_by_id(payload["sub"])
    if user is None:
        raise credentials_exception
    return user