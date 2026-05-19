from typing import List
from uuid import UUID
from fastapi import UploadFile

from backend.src.data.repository.room_repository import RoomRepository
from backend.src.data.schemas.room_schema import RoomCreateSchema


class RoomService:
    def __init__(self, room_repository: RoomRepository):
        self.room_repository = room_repository

    async def create_room(
        self,
        payload: RoomCreateSchema,
        images: List[UploadFile],
        current_user_id: UUID
    ) -> None:
        # todo
        pass