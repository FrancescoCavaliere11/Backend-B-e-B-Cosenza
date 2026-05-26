from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from typing import Annotated, List

from src.config.database_config import get_async_session
from src.data.repository.room_service_repository import RoomServiceRepository
from src.service.room_service_service import RoomServiceService
from src.data.schemas.room_service_schema import RoomServiceCreateSchema, RoomServiceSchema, RoomServiceUpdateSchema
from src.security.authorization import is_admin_user
from src.data.model.user import User

room_service_router = APIRouter(prefix="/api/v1/room-service", tags=["Room Service"])

async def get_room_service_service(db: AsyncSession = Depends(get_async_session)):
    repo = RoomServiceRepository(db)
    return RoomServiceService(repo)

@room_service_router.post("/", status_code=status.HTTP_201_CREATED, response_model=RoomServiceSchema)
async def create_room_service(
    payload: RoomServiceCreateSchema,
    service: Annotated[RoomServiceService, Depends(get_room_service_service)],
    current_user: Annotated[User, Depends(is_admin_user)]
):
    return await service.create_room_service(payload=payload, current_user_id=current_user.id)


@room_service_router.get("/", status_code=status.HTTP_200_OK, response_model=List[RoomServiceSchema])
async def get_all(
    room_service_service: Annotated[RoomServiceService, Depends(get_room_service_service)],
    current_user: User = Depends(is_admin_user)
) -> List[RoomServiceSchema]:
     return await room_service_service.get_all()


@room_service_router.put("/", status_code=status.HTTP_200_OK, response_model=RoomServiceSchema)
async def update_room_service(
    payload: RoomServiceUpdateSchema,
    room_service_service: Annotated[RoomServiceService, Depends(get_room_service_service)],
    current_user: User = Depends(is_admin_user)
) -> RoomServiceSchema:
    return await room_service_service.update_room_service(payload=payload, current_user_id=current_user.id)


@room_service_router.delete("/{room_service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room_service(
    room_service_id: UUID,
    room_service_service: Annotated[RoomServiceService, Depends(get_room_service_service)],
    current_user: User = Depends(is_admin_user)
) -> None :
    await room_service_service.delete_room_service(room_service_id=room_service_id)