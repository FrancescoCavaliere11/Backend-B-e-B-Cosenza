from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from backend.src.data.repository.user_repository import UserRepository
from backend.src.data.schemas.user_schema import UserCreateSchema
from backend.src.data.model.user import User
from backend.src.security.password_handler import get_password_hash
from backend.src.data.enumerators import UserRole
from backend.src.security.audit_logging import apply_audit_fields


class UserService:
    def __init__(self, user_repository: UserRepository):
        self.user_repository = user_repository

    async def register_new_user(self, payload: UserCreateSchema) -> None:
        user_data = payload.model_dump()
        user_data["password"] = get_password_hash(user_data.pop("password"))
        new_user = User(**user_data, role=UserRole.user)
        apply_audit_fields(new_user)

        try:
            await self.user_repository.save(new_user)
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email o numero di telefono già registrati nel sistema."
            )