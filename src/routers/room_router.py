from uuid import UUID
from fastapi import APIRouter, status, Form, UploadFile, File, Depends
from typing import Annotated, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
import json

from src.config.database_config import get_async_session
from src.data.model.user import User
from src.data.repository.room_repository import RoomRepository
from src.data.repository.room_service_repository import RoomServiceRepository
from src.data.schemas.room_schema import RoomCreateSchema, RoomSchema, RoomUpdateSchema
from src.service.room_service import RoomService
from src.security.authorization import is_admin_user


room_router = APIRouter(prefix="/api/v1/room", tags=["Room"])


async def get_room_service(db: AsyncSession = Depends(get_async_session)):
    room_repo = RoomRepository(db)
    room_service_repo = RoomServiceRepository(db)
    return RoomService(
        room_repository=room_repo,
        room_service_repository=room_service_repo,
        session=db
    )

# todo vedere se proteggere l'endpoint e farne uno a parte per i customer con meno dati
@room_router.get("/", response_model=List[RoomSchema])
async def get_all(
        room_service: Annotated[RoomService, Depends(get_room_service)],
) -> List[RoomSchema]:
    return await room_service.get_all()



@room_router.post("/", status_code=status.HTTP_201_CREATED, response_model=RoomSchema)
async def create_room(
    room_form: Annotated[str, Form()],
    image: Annotated[UploadFile, File()],
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User,Depends(is_admin_user)]
) -> RoomSchema:
    room_data_json = json.loads(room_form)
    payload = RoomCreateSchema(** room_data_json)
    return await service.create_room(payload=payload, image=image, current_user_id=current_user.id)


@room_router.put("/", status_code=status.HTTP_200_OK, response_model=RoomSchema)
async def update_room(
    room_form: Annotated[str, Form()],
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User, Depends(is_admin_user)],
    image: Annotated[Optional[UploadFile], File()] = None,
) -> RoomSchema:
    room_data_json = json.loads(room_form)
    payload = RoomUpdateSchema(**room_data_json)
    return await service.update_room(payload=payload, image=image, current_user_id=current_user.id)


@room_router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    room_id: UUID,
    service: Annotated[RoomService, Depends(get_room_service)],
    current_user: Annotated[User, Depends(is_admin_user)]
) -> None:
    await service.delete_room(room_id=room_id)
