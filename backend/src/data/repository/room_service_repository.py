from sqlalchemy import select
from typing import Optional, List
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.src.data.model.room_service import RoomService

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