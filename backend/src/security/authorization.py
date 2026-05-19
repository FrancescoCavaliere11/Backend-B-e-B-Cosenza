from backend.src.security.authentication import get_current_user
from fastapi import HTTPException, Depends, status
from typing import Annotated, List

from backend.src.data.model.user import User
from backend.src.data.enumerators import UserRole

class RoleChecker:
    def __init__(self, allowed_roles: List[UserRole]):
        self.allowed_roles = allowed_roles

    def __call__(self, current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operazione non autorizzata per questo ruolo."
            )
        return current_user


is_admin_user = RoleChecker(allowed_roles=[UserRole.admin])