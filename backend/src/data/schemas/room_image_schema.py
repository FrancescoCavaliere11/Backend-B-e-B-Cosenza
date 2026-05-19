from pydantic import Field

from backend.src.config.schemas_config import CustomModel

class RoomImageCreateSchema(CustomModel):
    is_primary: bool = Field(default=False)
