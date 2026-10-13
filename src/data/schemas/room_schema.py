from pydantic import ConfigDict, Field, field_validator
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from src.config.schemas_config import CustomModel
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.security.validators import validate_room_description, validate_room_name, validate_room_services_ids

#: Lunghezza massima della descrizione di una camera (testo libero del sito).
ROOM_DESCRIPTION_MAX_LENGTH = 1000


class RoomFormSchema(CustomModel):
    """Campi e regole comuni a creazione e modifica di una camera."""

    name: str = Field(min_length=2, max_length=100)

    capacity: int = Field(gt=0, le=20)

    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)

    room_services_ids: List[UUID] = Field(default_factory=list, max_length=50)

    number: int = Field(gt=0, le=1000)

    enabled: bool = Field(default=True)

    #: Facoltativa. Assente o vuota → `None`.
    description: Optional[str] = Field(default=None, max_length=ROOM_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str):
        return validate_room_name(value)

    @field_validator("room_services_ids")
    @classmethod
    def validate_room_services_ids(cls, value: List[UUID]):
        return validate_room_services_ids(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: Optional[str]) -> Optional[str]:
        return validate_room_description(value)


class RoomCreateSchema(RoomFormSchema):
    pass


class RoomUpdateSchema(RoomFormSchema):
    """
    Modifica completa della camera. Attenzione: `description` assente vale
    `None` e cancella quella salvata, come per gli altri campi facoltativi
    del form — il client manda sempre il form intero.
    """

    id: UUID = Field()


class RoomSchema(CustomModel):
    id: UUID
    name: str
    capacity: int
    price: Decimal
    number: int
    enabled: bool
    description: Optional[str] = None
    services: List[RoomServiceSchema]

    model_config = ConfigDict(from_attributes=True)
