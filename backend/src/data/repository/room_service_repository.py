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
