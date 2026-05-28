from dns.e164 import query
from sqlalchemy import select
from typing import List, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from src.data.model.room_service import RoomService

class RoomServiceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session


    async def save(self, room_service: RoomService) -> RoomService:
        try:
            self.session.add(room_service)
            await self.session.commit()
            await self.session.refresh(room_service)
            return room_service
        except IntegrityError as e:
            await self.session.rollback()
            raise e


    async def get_all_by_id(self, room_services_ids: List[UUID]) -> List[RoomService]:
        query = select(RoomService).where(RoomService.id.in_(room_services_ids))
        result = await self.session.execute(query)
        return list(result.scalars().all())


    async def get_all(self):
        query = select(RoomService)
        result = await self.session.execute(query)
        return list(result.scalars().all())


    async def update(self, room_service: RoomService) -> Optional[RoomService]:
        try:
            updated_room_service = await self.session.merge(room_service)
            await self.session.commit()
            await self.session.refresh(updated_room_service)
            return updated_room_service

        except IntegrityError as e:
            await self.session.rollback()
            raise e


    async def exists_by_id(self, room_service_id: UUID) -> Optional[RoomService]:
        query = select(RoomService).where(RoomService.id == room_service_id)
        result = await self.session.execute(query)
        return result.scalars().one_or_none()


    async def exists_by_name_excluding_id(self, name: str, room_service_id: UUID) -> bool:
        query = select(RoomService).where(
            RoomService.name == name,
            RoomService.id != room_service_id
        )
        result = await self.session.execute(query)
        existing_service = result.scalar_one_or_none()
        return existing_service is not None


    async def exists_by_name(self, name: str) -> bool:
        query = select(RoomService).where(RoomService.name == name)
        result = await self.session.execute(query)
        existing_service = result.scalar_one_or_none()
        return existing_service is not None


    async def delete_by_id(self, room_service_id: UUID) -> bool:
        try:
            room_service = await self.session.get(RoomService, room_service_id)
            if not room_service:
                return False
            await self.session.delete(room_service)
            await self.session.commit()
            return True
        except IntegrityError as e:
            await self.session.rollback()
            raise e