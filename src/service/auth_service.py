from typing import Optional
from fastapi import HTTPException, status

from src.data.repository.user_repository import UserRepository
from src.data.model.user import User
from src.security.password_handler import verify_dummy_password, verify_password
from src.security.jwt_handler import create_access_token, create_refresh_token
from src.data.schemas.auth_schema import AuthResponseSchema


class AuthService:
    def __init__(self, user_repository: UserRepository):
        self.user_repository = user_repository

    async def authenticate_user(self, email: str, password: str):
        unauthorized_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="email o password errati",
            headers={"WWW-Authenticate": "Bearer"},
        )

        user: Optional[User] = await self.user_repository.get_by_email(email)
        if user is None:
            verify_dummy_password(password)
            raise unauthorized_exception
        if not verify_password(password, user.password):
            raise unauthorized_exception

        access_token = create_access_token(user)
        refresh_token = create_refresh_token(user)
        return AuthResponseSchema(access_token=access_token, refresh_token=refresh_token, token_type="bearer")