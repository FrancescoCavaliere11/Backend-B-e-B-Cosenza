from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.data.model.room_image import RoomImage


class RoomRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(self, room_image: RoomImage) -> RoomImage:
        try:
            self.session.add(room_image)
            await self.session.commit()
            await self.session.refresh(room_image)
            return room_image
        except IntegrityError as e:
            await self.session.rollback()
            raise e