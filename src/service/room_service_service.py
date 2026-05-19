from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status
from uuid import UUID

from src.data.model.room_service import RoomService
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_service_schema import RoomServiceCreateSchema
from src.security.audit_logging import apply_audit_fields

class RoomServiceService:
    def __init__(self, room_service_repository: RoomServiceRepository):
        self.room_service_repository = room_service_repository

    async def create_room_service(self, payload: RoomServiceCreateSchema, current_user_id: UUID) -> None:
        room_service_data = payload.model_dump()
        new_room_service = RoomService(** room_service_data)
        apply_audit_fields(audit=new_room_service, user_id=current_user_id, is_create=True)

        try:
            await self.room_service_repository.save(new_room_service)
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esiste già un servizio con lo stesso nome"
            )