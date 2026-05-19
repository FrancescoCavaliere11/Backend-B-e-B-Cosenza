from enum import Enum


class UserRole(str, Enum):
    user = "user"
    admin = "admin"


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"