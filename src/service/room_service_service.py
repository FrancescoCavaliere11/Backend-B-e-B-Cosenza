from typing import List

from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status
from uuid import UUID

from src.data.model.room_service import RoomService
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_service_schema import RoomServiceCreateSchema, RoomServiceSchema, RoomServiceUpdateSchema
from src.exception.custom_exception import EntityNotFound, EntityAlreadyExists
from src.security.audit_logging import apply_audit_fields

class RoomServiceService:
    def __init__(self, room_service_repository: RoomServiceRepository):
        self.room_service_repository = room_service_repository

    async def create_room_service(
        self,
        payload: RoomServiceCreateSchema,
        current_user_id: UUID
    ) -> RoomServiceSchema:
        room_service_data = payload.model_dump()
        new_room_service = RoomService(** room_service_data)

        if await self.room_service_repository.exists_by_name(new_room_service.name):
            raise EntityAlreadyExists(message="Esiste già un servizio con questo nome")

        apply_audit_fields(audit=new_room_service, user_id=current_user_id, is_create=True)

        room_service = await self.room_service_repository.save(new_room_service)
        room_service_schema = RoomServiceSchema.model_validate(room_service)
        return room_service_schema


    async def get_all(self) -> List[RoomServiceSchema]:
        room_services = await self.room_service_repository.get_all()
        room_services_schema = [RoomServiceSchema.model_validate(service) for service in room_services]
        return room_services_schema


    async def update_room_service(
        self,
        payload: RoomServiceUpdateSchema,
        current_user_id: UUID
    ) -> RoomServiceSchema:
        room_service_data = payload.model_dump()
        updated_room_service = RoomService(** room_service_data)

        if not await self.room_service_repository.exists_by_id(updated_room_service.id):
            raise EntityNotFound(message="Il servizio non esiste")

        if await self.room_service_repository.exists_by_name_excluding_id(
                updated_room_service.name,
                updated_room_service.id
        ):
            raise EntityAlreadyExists(message="Esiste già un servizio con questo nome")

        apply_audit_fields(audit=updated_room_service, user_id=current_user_id)

        room_service = await self.room_service_repository.update(updated_room_service)
        if room_service is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Il servizio non esiste"
            )

        room_service_schema = RoomServiceSchema.model_validate(room_service)
        return room_service_schema


    async def delete_room_service(self, room_service_id: UUID) -> None:
        room_service = await self.room_service_repository.delete_by_id(room_service_id)
        if not room_service:
            raise EntityNotFound(message="Il servizio non esiste")
