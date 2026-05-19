from pydantic import Field, field_validator, model_validator
from decimal import Decimal
from typing import List
from uuid import UUID

from src.config.schemas_config import CustomModel
from src.security.validators import validate_room_name, validate_room_images, validate_room_services_ids
from src.data.schemas.room_image_schema import RoomImageCreateSchema

class RoomCreateSchema(CustomModel):
    name: str = Field(min_length=2, max_length=100)

    capacity: int = Field(gt=0, le=20)

    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)

    room_images: List[RoomImageCreateSchema] = Field(default_factory=list)

    room_services_ids: List[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        return validate_room_name(value)

    @field_validator("room_services_ids")
    @classmethod
    def validate_room_services_ids(cls, value: List[UUID]):
        return validate_room_services_ids(value)

    @model_validator(mode="after")
    def check_images(self) -> "RoomCreateSchema":
        validate_room_images(self.room_images)
        return self