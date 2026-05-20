from pydantic import Field
from uuid import UUID

from backend.src.config.schemas_config import CustomModel

class RoomImageCreateSchema(CustomModel):
    client_image_id: UUID = Field()
    is_primary: bool = Field(default=False)
