from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.src.data.model.room import Room

class RoomRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, room: Room) -> Room:
        try:
            self.session.add(room)
            await self.session.commit()
            await self.session.refresh(room)
            return room
        except IntegrityError as e:
            await self.session.rollback()
            raise e