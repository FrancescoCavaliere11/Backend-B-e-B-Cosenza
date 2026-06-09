from pydantic import Field, field_validator, model_validator
from decimal import Decimal
from typing import List
from uuid import UUID

from src.config.schemas_config import CustomModel
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.security.validators import validate_room_name, validate_room_services_ids

class RoomCreateSchema(CustomModel):
    name: str = Field(min_length=2, max_length=100)

    capacity: int = Field(gt=0, le=20)

    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)

    room_services_ids: List[UUID] = Field(default_factory=list, max_length=50)

    number: int = Field(gt=0, le=1000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        return validate_room_name(value)

    @field_validator("room_services_ids")
    @classmethod
    def validate_room_services_ids(cls, value: List[UUID]):
        return validate_room_services_ids(value)


class RoomUpdateSchema(CustomModel):
    id: UUID = Field()
    name: str = Field(min_length=2, max_length=100)
    capacity: int = Field(gt=0, le=20)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    room_services_ids: List[UUID] = Field(default_factory=list, max_length=50)
    number: int = Field(gt=0, le=1000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        return validate_room_name(value)

    @field_validator("room_services_ids")
    @classmethod
    def validate_room_services_ids(cls, value: List[UUID]):
        return validate_room_services_ids(value)


class RoomSchema(CustomModel):
    id: UUID
    name: str
    capacity: int
    price: Decimal
    number: int
    services: List[RoomServiceSchema]

    class Config:
        from_attributes = True
