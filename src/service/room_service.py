from typing import List
from uuid import UUID
from fastapi import UploadFile, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.room import Room
from src.data.model.room_service import RoomService as RoomServiceModel
from src.data.repository.room_repository import RoomRepository
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_schema import RoomCreateSchema, RoomSchema
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.exception.custom_exception import EntityAlreadyExists, InvalidRoomService
from src.security.audit_logging import apply_audit_fields

class RoomService:
    def __init__(
            self,
            room_repository: RoomRepository,
            room_service_repository: RoomServiceRepository,
            session: AsyncSession
    ):
        self.room_repository = room_repository
        self.room_service_repository = room_service_repository
        self.session = session


    async def get_all(self) -> List[RoomSchema]:
        room_services = await self.room_repository.get_all()
        room_services_schemas = []
        for room_service in room_services:
            room_data = {
                "id": room_service.id,
                "name": room_service.name,
                "capacity": room_service.capacity,
                "price": room_service.price,
                "number": room_service.number,
                "services": [RoomServiceSchema.model_validate(service) for service in room_services]
            }

            room_services_schema = RoomSchema.model_validate(room_data)
            room_services_schemas.append(room_services_schema)

        return room_services_schemas


    async def create_room(
            self,
            payload: RoomCreateSchema,
            image: UploadFile,
            current_user_id: UUID
    ) -> RoomSchema:
        if self.session.in_transaction():
            return await self._execute_create_room(payload=payload, image=image, current_user_id=current_user_id)

        async with self.session.begin():
            return await self._execute_create_room(payload=payload, image=image, current_user_id=current_user_id)



    async def _execute_create_room(
            self,
            payload: RoomCreateSchema,
            image: UploadFile,
            current_user_id: UUID
    ) -> RoomSchema:
        room_services = await self._validate_and_get_services(payload.room_services_ids)

        if await self.room_repository.get_by_name(payload.name) is not None:
            raise EntityAlreadyExists("Esiste già una stanza con questo nome")

        if await self.room_repository.get_by_number(payload.number) is not None:
            raise EntityAlreadyExists("Esiste già una stanza con questo numero")

        img_url = ""  # todo modificare quando implemento il servizio per caricare le immagini

        new_room = Room(
            name=payload.name,
            capacity=payload.capacity,
            number=payload.number,
            price=payload.price,
            services=room_services,
            img_url=img_url
        )

        apply_audit_fields(audit=new_room, user_id=current_user_id, is_create=True)
        self.session.add(new_room)

        await self.session.flush()
        await self.session.refresh(new_room)

        room_data = {
            "id": new_room.id,
            "name": new_room.name,
            "capacity": new_room.capacity,
            "price": new_room.price,
            "number": new_room.number,
            "services": [RoomServiceSchema.model_validate(service) for service in room_services]
        }

        await self.session.commit()
        return RoomSchema.model_validate(room_data)

    async def _validate_and_get_services(self, service_ids: List[UUID]) -> List[RoomServiceModel]:
        room_services = await self.room_service_repository.get_all_by_id(service_ids)
        if len(room_services) != len(service_ids):
            raise InvalidRoomService("Uno o più servizi inseriti non sono validi")
        return list(room_services)
