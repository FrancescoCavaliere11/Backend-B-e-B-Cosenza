from pydantic import Field, field_validator
from uuid import UUID

from src.config.schemas_config import CustomModel
from src.security.validators import validate_room_services_name

class RoomServiceCreateSchema(CustomModel):
    name: str = Field(min_length=2, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_room_services_name(value)


class RoomServiceSchema(CustomModel):
    id: UUID
    name: str

    model_config = {
        "from_attributes": True
    }