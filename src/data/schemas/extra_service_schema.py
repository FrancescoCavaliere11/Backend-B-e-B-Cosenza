from typing import Optional
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from src.config.schemas_config import CustomModel
from src.security.validators import validate_extra_service_name, validate_extra_service_description


class ExtraServiceSchema(CustomModel):
    id: UUID
    name: str
    img_url: str
    description: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class ExtraServiceFormSchema(CustomModel):
    """Campi e regole comuni a creazione e modifica di un servizio extra."""

    name: str = Field(min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=200)

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_extra_service_name(value)

    @field_validator('description')
    @classmethod
    def validate_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        return validate_extra_service_description(value)


class ExtraServiceCreateSchema(ExtraServiceFormSchema):
    """Creazione di un servizio extra."""


class ExtraServiceUpdateSchema(ExtraServiceFormSchema):
    """Modifica completa: il client manda sempre il form intero, più l'`id`."""

    id: UUID = Field()
