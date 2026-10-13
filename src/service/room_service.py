from typing import List, Optional, Sequence
from uuid import UUID
from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.room import Room
from src.data.model.room_service import RoomService as RoomServiceModel
from src.data.repository.room_repository import RoomRepository
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_schema import RoomCreateSchema, RoomSchema, RoomUpdateSchema
from src.data.schemas.room_service_schema import RoomServiceSchema
from src.exception.custom_exception import EntityAlreadyExists, EntityInUse, InvalidRoomService, EntityNotFound
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
        return [self._to_schema(room, room.services) for room in rooms]

    @staticmethod
    def _to_schema(room: Room, services: Sequence[RoomServiceModel]) -> RoomSchema:
        """
        Vista di una camera. I servizi si passano a parte: dopo un `refresh`
        la relazione non è caricata, e leggerla qui in un contesto async
        scatenerebbe un lazy load non consentito.
        """
        return RoomSchema(
            id=room.id,
            name=room.name,
            capacity=room.capacity,
            price=room.price,
            number=room.number,
            enabled=room.enabled,
            description=room.description,
            services=[RoomServiceSchema.model_validate(service) for service in services],
        )


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
            img_url=img_url,
            enabled=payload.enabled,
            description=payload.description,
        )

        apply_audit_fields(audit=new_room, user_id=current_user_id, is_create=True)
        self.session.add(new_room)

        await self.session.flush()
        await self.session.refresh(new_room)

        room_schema = self._to_schema(new_room, room_services)

        await self.session.commit()
        return room_schema

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
        room.enabled = payload.enabled
        room.description = payload.description

        if image is not None:
            await validate_image(image)
            room.img_url = ""  # todo: caricare l'immagine nel servizio di storage in cloud

        apply_audit_fields(audit=room, user_id=current_user_id)

        await self.session.flush()
        await self.session.refresh(room)

        room_schema = self._to_schema(room, room_services)

        await self.session.commit()
        return room_schema

    async def delete_room(self, room_id: UUID) -> None:
        # todo: quando elimino, va eliminata anche l'immagine relativa alla stanza dallo store che userò
        try:
            room_deleted = await self.room_repository.delete_by_id(room_id)
        except IntegrityError as error:
            # La foreign key `booking_room_items.room_id` è ON DELETE RESTRICT:
            # una camera con prenotazioni associate non è cancellabile, perché
            # eliminarla distruggerebbe lo storico contabile di quei soggiorni.
            # Prima dello Step A questo caso produceva un 500.
            # 
            raise EntityInUse(
                "La camera ha prenotazioni associate e non può essere eliminata, "
                "nemmeno se disattivata: le prenotazioni esistenti devono restare "
                "consultabili. Per non riceverne di nuove, disattivala: non comparirà "
                "più fra le camere prenotabili."
            ) from error

        if not room_deleted:
            raise EntityNotFound("La stanza non esiste")
