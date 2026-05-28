import json
from typing import List, Optional
from uuid import UUID

from fastapi import UploadFile, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.extra_service import ExtraService
from src.data.repository.extra_service_repository import ExtraServiceRepository
from src.data.schemas.extra_service_schema import ExtraServiceCreateSchema, ExtraServiceSchema, ExtraServiceUpdateSchema
from src.security.audit_logging import apply_audit_fields
from src.security.validators import validate_image


class ExtraServiceService:
    def __init__(
            self,
            extra_service_repository: ExtraServiceRepository,
            session: AsyncSession
    ) -> None:
        self.extra_service_repository = extra_service_repository
        self.session = session


    async def create_extra_service(
            self,
            payload: ExtraServiceCreateSchema,
            image: UploadFile,
            current_user_id: UUID
    ) -> ExtraServiceSchema:

        await validate_image(image)
        img_url = "https://placeholder.com"  # todo upload cloud

        new_service = ExtraService(
            name=payload.name,
            img_url=img_url,
            description=payload.description,
        )

        apply_audit_fields(audit=new_service, user_id=current_user_id, is_create=True)

        await self.extra_service_repository.create(new_service)

        extra_service_schema = ExtraServiceSchema.model_validate(new_service)
        return extra_service_schema


    async def update_extra_service(
            self,
            payload: ExtraServiceUpdateSchema,
            image: Optional[UploadFile],
            current_user_id: UUID
    ):
        if image is not None:
            await validate_image(image)
            # todo aggiornare immagine nel cloude e nel

        extra_service_data = payload.model_dump()
        updated_extra_service = ExtraService(**extra_service_data)
        apply_audit_fields(audit=updated_extra_service, user_id=current_user_id)

        extra_service = await self.extra_service_repository.update(updated_extra_service)
        if extra_service is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Il servizio non esiste"
            )

        extra_service_schema = ExtraServiceSchema.model_validate(extra_service)
        return extra_service_schema


    async def delete_extra_service(self, extra_service_id: UUID) -> None:
        extra_service = await self.extra_service_repository.delete(extra_service_id)
        # todo eliminare l'immagine del cloude
        if not extra_service:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Il servizio non esiste"
            )


    async def get_all(self) -> List[ExtraServiceSchema]:
        extra_services = await self.extra_service_repository.get_all()
        extra_services_schema = [ExtraServiceSchema.model_validate(service) for service in extra_services]
        return extra_services_schema


