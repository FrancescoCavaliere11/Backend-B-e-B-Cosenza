from typing import List, Optional
from uuid import UUID
from fastapi import UploadFile, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.room import Room
from src.data.model.room_service import RoomService as RoomServiceModel
from src.data.repository.room_repository import RoomRepository
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_schema import RoomCreateSchema, RoomSchema, RoomUpdateSchema
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.exception.custom_exception import EntityAlreadyExists, InvalidRoomService, EntityNotFound
from src.security.audit_logging import apply_audit_fields
from src.security.validators import validate_image

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
        rooms = await self.room_repository.get_all()
        room_schemas = []
        for room in rooms:
            room_data = {
                "id": room.id,
                "name": room.name,
                "capacity": room.capacity,
                "price": room.price,
                "number": room.number,
                "services": [RoomServiceSchema.model_validate(service) for service in room.services]
            }

            room_schema = RoomSchema.model_validate(room_data)
            room_schemas.append(room_schema)

        return room_schemas


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
        await validate_image(image)
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


    async def update_room(
            self,
            payload: RoomUpdateSchema,
            image: Optional[UploadFile],
            current_user_id: UUID
    ) -> RoomSchema:
        if self.session.in_transaction():
            return await self._execute_update_room(payload=payload, image=image, current_user_id=current_user_id)

        async with self.session.begin():
            return await self._execute_update_room(payload=payload, image=image, current_user_id=current_user_id)


    async def _execute_update_room(
            self,
            payload: RoomUpdateSchema,
            image: Optional[UploadFile],
            current_user_id: UUID
    ) -> RoomSchema:
        room = await self.room_repository.get_by_id(payload.id)
        if room is None:
            raise EntityNotFound("La stanza non esiste")

        if await self.room_repository.exists_by_name_excluding_id(payload.name, payload.id):
            raise EntityAlreadyExists("Esiste già una stanza con questo nome")

        if await self.room_repository.exists_by_number_excluding_id(payload.number, payload.id):
            raise EntityAlreadyExists("Esiste già una stanza con questo numero")

        room_services = await self._validate_and_get_services(payload.room_services_ids)

        room.name = payload.name
        room.capacity = payload.capacity
        room.number = payload.number
        room.price = payload.price
        room.services = room_services

        if image is not None:
            await validate_image(image)
            room.img_url = ""  # todo: caricare l'immagine nel servizio di storage in cloud

        apply_audit_fields(audit=room, user_id=current_user_id)

        await self.session.flush()
        await self.session.refresh(room)

        room_data = {
            "id": room.id,
            "name": room.name,
            "capacity": room.capacity,
            "price": room.price,
            "number": room.number,
            "services": [RoomServiceSchema.model_validate(service) for service in room_services]
        }

        await self.session.commit()
        return RoomSchema.model_validate(room_data)
