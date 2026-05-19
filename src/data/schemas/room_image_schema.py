from pydantic import Field, field_validator

from src.config.schemas_config import CustomModel

class RoomImageCreateSchema(CustomModel):
    is_primary: bool = Field(default=False)
