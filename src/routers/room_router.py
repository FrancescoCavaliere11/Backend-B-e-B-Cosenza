from fastapi import APIRouter, status, Form, UploadFile, File, Depends, HTTPException
from typing import Annotated, List

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
import json

from src.config.database_config import get_async_session
from src.data.model.user import User
from src.data.repository.room_repository import RoomRepository
from src.data.schemas.room_schema import RoomCreateSchema
from src.service.room_service import RoomService
from src.security.authorization import is_admin_user

room_router = APIRouter(prefix="/room", tags=["Room"])

async def get_room_service(db: AsyncSession = Depends(get_async_session)):
    repo = RoomRepository(db)
    return RoomService(repo)

@room_router.post("/", status_code=status.HTTP_201_CREATED)
async def create_room(
    room_form: Annotated[str, Form()],
    images: Annotated[List[UploadFile], File()],
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User,Depends(is_admin_user)]
):
    try:
        room_data_json = json.loads(room_form)
        payload = RoomCreateSchema(** room_data_json)
    except(json.JSONDecodeError, ValidationError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"I dati della stanza non sono validi: {err}"
        )

    return await service.create_room(payload, images,)

