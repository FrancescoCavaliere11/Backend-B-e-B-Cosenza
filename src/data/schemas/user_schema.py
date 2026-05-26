from uuid import UUID
from pydantic import Field, EmailStr, field_validator

from src.config.schemas_config import CustomModel
from src.data.enumerators import UserRole
from src.security.validators import validate_phone_number, validate_password_strength, validate_user_lastname, validate_user_firstname


class UserCreateSchema(CustomModel):
    email: EmailStr

    # almeno una maiuscola, almeno una minuscola, almeno un numero almeno un carattere speciale lunghezza minima
    password: str = Field(min_length=8,max_length=20)

    firstname: str = Field(min_length=2,max_length=50)

    lastname: str = Field(min_length=2,max_length=50)

    phone_number: str = Field(min_length=10,max_length=10)

    @field_validator("firstname")
    @classmethod
    def validate_firstname(cls, value: str):
        return validate_user_firstname(value)

    @field_validator("lastname")
    @classmethod
    def validate_lastname(cls, value: str):
        return validate_user_lastname(value)

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, value: str):
        return validate_phone_number(value)

    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, value: str):
        return validate_password_strength(value)


class CurrentUserSchema(CustomModel):
    id: UUID
    email: str
    firstname: str
    role: UserRole

    class Config:
        from_attributes = True

