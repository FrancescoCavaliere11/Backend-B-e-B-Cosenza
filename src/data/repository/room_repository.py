from typing import Optional, List
from uuid import UUID

from dns.e164 import query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from src.data.model.room import Room

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


    async def get_by_name(self, name: str) -> Optional[Room]:
        query = select(Room).where(Room.name == name)
        result = await self.session.execute(query)
        room = result.scalar_one_or_none()
        return room


    async def get_by_number(self, number: int) -> Optional[Room]:
        query = select(Room).where(Room.number == number)
        result = await self.session.execute(query)
        room = result.scalar_one_or_none()
        return room


    async def get_all(self) -> List[Room]:
        query = select(Room).options(selectinload(Room.services)).order_by(Room.number.asc())
        result = await self.session.execute(query)
        return list(result.scalars().all())


    async def get_by_id(self, room_id: UUID) -> Optional[Room]:
        query = select(Room).options(selectinload(Room.services)).where(Room.id == room_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()


    async def exists_by_name_excluding_id(self, name: str, room_id: UUID) -> bool:
        query = select(Room).where(
            Room.name == name,
            Room.id != room_id
        )
        result = await self.session.execute(query)
        existing_room = result.scalar_one_or_none()
        return existing_room is not None


    async def exists_by_number_excluding_id(self, number: int, room_id: UUID) -> bool:
        query = select(Room).where(
            Room.number == number,
            Room.id != room_id
        )
        result = await self.session.execute(query)
        existing_room = result.scalar_one_or_none()
        return existing_room is not None


    async def update(self, room: Room) -> Room:
        try:
            updated_room = await self.session.merge(room)
            await self.session.commit()
            await self.session.refresh(updated_room)
            return updated_room
        except IntegrityError as e:
            await self.session.rollback()
            raise e